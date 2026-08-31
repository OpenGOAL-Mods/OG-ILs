#include "replay_client.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <mutex>
#include <optional>
#include <queue>
#include <random>
#include <stdexcept>
#include <thread>
#include <utility>
#include <vector>

#include "common/log/log.h"
#include "common/util/FileUtil.h"
#include "common/util/json_util.h"
#include "common/versions/versions.h"

#include "game/runtime.h"

#include "curl/curl.h"
#include "fmt/format.h"
#include "third-party/imgui/imgui.h"

namespace replay_client {
namespace {

#ifndef OPENGOAL_REPLAY_DEFAULT_URL
#define OPENGOAL_REPLAY_DEFAULT_URL "https://opengoal-replays.sparked.network"
#endif

#ifndef OPENGOAL_REPLAY_DEFAULT_GAME_TOKEN
#define OPENGOAL_REPLAY_DEFAULT_GAME_TOKEN ""
#endif

constexpr const char* kDefaultServerUrl = OPENGOAL_REPLAY_DEFAULT_URL;
constexpr const char* kDefaultGameToken = OPENGOAL_REPLAY_DEFAULT_GAME_TOKEN;
constexpr int kMaxSelectedReplays = 33;

struct ServerConfig {
  std::string url = kDefaultServerUrl;
  std::string game_token = kDefaultGameToken;
};

std::mutex g_server_config_mutex;

std::string normalized_server_url(std::string url) {
  while (!url.empty() && std::isspace(static_cast<unsigned char>(url.front()))) {
    url.erase(url.begin());
  }
  while (!url.empty() && std::isspace(static_cast<unsigned char>(url.back()))) {
    url.pop_back();
  }
  while (!url.empty() && url.back() == '/') {
    url.pop_back();
  }
  if (!url.starts_with("http://") && !url.starts_with("https://")) {
    throw std::runtime_error("Replay server URL must start with http:// or https://");
  }
  return url;
}

fs::path server_config_path() {
  return file_util::get_user_features_dir(g_game_version) / "replay-server.json";
}

ServerConfig load_server_config() {
  ServerConfig config;
  try {
    const auto path = server_config_path();
    if (fs::exists(path)) {
      if (const auto parsed = safe_parse_json(file_util::read_text_file(path))) {
        config.url = parsed->value("url", config.url);
        config.game_token = parsed->value("game_token", config.game_token);
      }
    }
  } catch (const std::exception& error) {
    lg::warn("Could not load replay server settings: {}", error.what());
  }
  if (const auto* environment_url = std::getenv("OPENGOAL_REPLAY_SERVER_URL")) {
    config.url = environment_url;
  }
  if (const auto* environment_token = std::getenv("OPENGOAL_REPLAY_GAME_TOKEN")) {
    config.game_token = environment_token;
  }
  try {
    config.url = normalized_server_url(config.url);
  } catch (const std::exception&) {
    config.url = kDefaultServerUrl;
  }
  return config;
}

ServerConfig& mutable_server_config() {
  static ServerConfig config = load_server_config();
  return config;
}

ServerConfig server_config_snapshot() {
  std::lock_guard lock(g_server_config_mutex);
  return mutable_server_config();
}

void save_server_config(const std::string& url, const std::string& game_token) {
  ServerConfig config{normalized_server_url(url), game_token};
  file_util::write_text_file(server_config_path(),
                             json{{"url", config.url}, {"game_token", config.game_token}}.dump(2));
  std::lock_guard lock(g_server_config_mutex);
  mutable_server_config() = std::move(config);
}

bool valid_player_id(const std::string& value) {
  return value.size() == 32 && std::all_of(value.begin(), value.end(), [](unsigned char character) {
           return std::isxdigit(character) != 0;
         });
}

std::string generate_player_id() {
  constexpr char kHex[] = "0123456789abcdef";
  std::array<unsigned char, 16> bytes{};
  std::random_device random;
  for (auto& byte : bytes) {
    byte = static_cast<unsigned char>(random());
  }
  std::string result;
  result.reserve(bytes.size() * 2);
  for (const auto byte : bytes) {
    result.push_back(kHex[byte >> 4]);
    result.push_back(kHex[byte & 0x0f]);
  }
  return result;
}

const std::string& persistent_player_id() {
  static const std::string value = []() {
    const auto path = file_util::get_user_features_dir(g_game_version) / "replay-player-id.txt";
    try {
      if (fs::exists(path)) {
        auto stored = file_util::read_text_file(path);
        stored.erase(
            std::remove_if(stored.begin(), stored.end(),
                           [](unsigned char character) { return std::isspace(character) != 0; }),
            stored.end());
        if (valid_player_id(stored)) {
          std::transform(stored.begin(), stored.end(), stored.begin(), [](unsigned char character) {
            return static_cast<char>(std::tolower(character));
          });
          return stored;
        }
        lg::warn("Ignoring invalid replay player ID in {}", path.string());
      }
    } catch (const std::exception& error) {
      lg::warn("Could not read replay player ID from {}: {}", path.string(), error.what());
    }

    auto generated = generate_player_id();
    try {
      file_util::write_text_file(path, generated);
      lg::info("Created permanent replay player ID {}", generated);
    } catch (const std::exception& error) {
      lg::error("Could not persist replay player ID to {}: {}", path.string(), error.what());
    }
    return generated;
  }();
  return value;
}

struct ReplayInfo {
  std::string id;
  std::string display_name;
  std::string category;
  std::string src_status;
  std::string src_runner_id;
  std::string src_runner_name;
  bool completed = true;
  float time_seconds = 0.f;
};

struct ReplayModeInfo {
  std::string id;
  std::string label;
  std::string description;
};

std::vector<ReplayModeInfo> fallback_modes() {
  return {{"default", "Default - Next Place",
           "Start with the slowest ghost, then race the next faster time."},
          {"next_three", "Default - Next 3 Places",
           "Start with the three slowest ghosts, then race the three closest faster times."},
          {"personal_best", "Race vs Your Best", "Race your fastest completed replay."},
          {"world_record", "Race vs WR", "Race the fastest completed server replay."},
          {"last_attempt", "Race vs Last Attempt",
           "Race your newest attempt, including an unfinished retry."},
          {"custom", "Custom", "Race any manually selected mission replays."}};
}

struct RunnerInfo {
  std::string id;
  std::string display_name;
};

struct PointLeaderboardEntry {
  int rank = 0;
  std::string rank_label;
  std::string runner;
  int points = 0;
  int missions_run = 0;
  int missions_total = 0;
  int tied_wrs = 0;
  int untied_wrs = 0;
};

struct PointLeaderboardFilter {
  const char* id;
  const char* label;
};

constexpr std::array<PointLeaderboardFilter, 3> kPointLeaderboardModes = {
    PointLeaderboardFilter{"jak2", "Jak II"},
    PointLeaderboardFilter{"jak3", "Jak 3"},
    PointLeaderboardFilter{"combined", "Combined"},
};

constexpr std::array<PointLeaderboardFilter, 4> kPointLeaderboardGroups = {
    PointLeaderboardFilter{"all", "Overall"},
    PointLeaderboardFilter{"main", "Main Missions"},
    PointLeaderboardFilter{"orb", "Orb Searches"},
    PointLeaderboardFilter{"side", "Other Side Missions"},
};

std::string mapped_player_name(const json& state) {
  std::string runner_id;
  if (state.contains("players") && state.at("players").is_array()) {
    for (const auto& player : state.at("players")) {
      if (player.value("id", "") == persistent_player_id()) {
        runner_id = player.value("src_runner_id", "");
        break;
      }
    }
  }
  if (runner_id.empty() || !state.contains("runners") || !state.at("runners").is_array()) {
    return {};
  }
  for (const auto& runner : state.at("runners")) {
    if (runner.value("id", "") == runner_id) {
      return runner.value("display_name", "");
    }
  }
  return {};
}

struct HttpResponse {
  bool ok = false;
  long status = 0;
  std::string body;
  std::string error;
};

size_t write_callback(void* contents, size_t size, size_t count, void* target) {
  static_cast<std::string*>(target)->append(static_cast<char*>(contents), size * count);
  return size * count;
}

HttpResponse request(const std::string& method,
                     const std::string& path,
                     const std::string& body = {}) {
  static std::once_flag curl_init;
  std::call_once(curl_init, []() { curl_global_init(CURL_GLOBAL_DEFAULT); });
  HttpResponse response;
  CURL* curl = curl_easy_init();
  if (!curl) {
    response.error = "Could not initialize HTTP client";
    return response;
  }
  const auto config = server_config_snapshot();
  const auto url = fmt::format("{}{}", config.url, path);
  curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
  curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 10000L);
  curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 1000L);
  curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
  curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback);
  curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response.body);
  struct curl_slist* headers = nullptr;
  if (!config.game_token.empty()) {
    headers = curl_slist_append(headers,
                                fmt::format("Authorization: Bearer {}", config.game_token).c_str());
  }
  if (method != "GET") {
    curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, method.c_str());
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.c_str());
    curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(body.size()));
    headers = curl_slist_append(headers, "Content-Type: application/json");
  }
  if (headers) {
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
  }
  const auto result = curl_easy_perform(curl);
  curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &response.status);
  if (result != CURLE_OK) {
    response.error = curl_easy_strerror(result);
  } else if (response.status < 200 || response.status >= 300) {
    response.error = fmt::format("Replay server returned HTTP {}", response.status);
    if (const auto parsed = safe_parse_json(response.body); parsed && parsed->contains("error")) {
      response.error = parsed->at("error").get<std::string>();
    }
  } else {
    response.ok = true;
  }
  curl_slist_free_all(headers);
  curl_easy_cleanup(curl);
  return response;
}

class Client {
 public:
  Client() {
    const auto config = server_config_snapshot();
    std::strncpy(m_server_url.data(), config.url.c_str(), m_server_url.size() - 1);
    std::strncpy(m_game_token.data(), config.game_token.c_str(), m_game_token.size() - 1);
    try {
      file_util::create_dir_if_needed("ghost");
    } catch (const std::exception& error) {
      lg::warn("Could not create replay directory: {}", error.what());
    }
    m_worker = std::thread([this]() { worker_loop(); });
  }

  ~Client() {
    {
      std::lock_guard lock(m_job_mutex);
      m_stopping = true;
    }
    m_job_cv.notify_one();
    if (m_worker.joinable()) {
      m_worker.join();
    }
  }

  void enqueue(std::function<void()> job) {
    {
      std::lock_guard lock(m_job_mutex);
      m_jobs.push(std::move(job));
    }
    m_job_cv.notify_one();
  }

  void refresh() {
    {
      std::lock_guard lock(m_state_mutex);
      m_refresh_started = true;
      m_status = "Refreshing replay server...";
    }
    enqueue([this]() {
      auto response =
          request("GET", fmt::format("/api/state?player_id={}", persistent_player_id()));
      if (!response.ok) {
        set_connected(false, response.error);
        return;
      }
      const auto parsed = safe_parse_json(response.body);
      if (!parsed) {
        set_connected(false, "Replay server returned invalid JSON");
        return;
      }
      std::vector<ReplayInfo> replays;
      std::vector<RunnerInfo> runners;
      std::vector<ReplayModeInfo> modes;
      try {
        const auto player_name = mapped_player_name(*parsed);
        for (const auto& item : parsed->at("replays")) {
          replays.push_back({item.value("id", ""), item.value("display_name", "Unnamed replay"),
                             item.value("category", ""), item.value("src_status", ""),
                             item.value("src_runner_id", ""), item.value("src_runner_name", ""),
                             item.value("completed", true), item.value("time_seconds", 0.f)});
        }
        for (const auto& item : parsed->at("runners")) {
          runners.push_back({item.value("id", ""), item.value("display_name", "Unknown runner")});
        }
        if (parsed->contains("replay_modes") && parsed->at("replay_modes").is_array()) {
          for (const auto& item : parsed->at("replay_modes")) {
            modes.push_back({item.value("id", ""), item.value("label", "Unknown mode"),
                             item.value("description", "")});
          }
        }
        if (modes.empty()) {
          modes = fallback_modes();
        }
        const auto& settings = parsed->at("settings");
        std::vector<std::string> selected_replays;
        if (settings.contains("selected_replay_ids") &&
            settings.at("selected_replay_ids").is_array()) {
          for (const auto& replay_id : settings.at("selected_replay_ids")) {
            if (replay_id.is_string()) {
              selected_replays.push_back(replay_id.get<std::string>());
            }
          }
        } else {
          const auto selected_replay = settings.value("selected_replay_id", "");
          if (!selected_replay.empty()) {
            selected_replays.push_back(selected_replay);
          }
        }
        std::string active_category;
        int selection_revision = 0;
        {
          std::lock_guard lock(m_state_mutex);
          m_replays = std::move(replays);
          m_runners = std::move(runners);
          m_modes = std::move(modes);
          m_selected_replay_ids = selected_replays;
          m_replay_mode = settings.value("replay_mode", "default");
          m_player_name = player_name;
          m_identity_refresh_pending = false;
          m_next_identity_refresh = std::chrono::steady_clock::now() + std::chrono::seconds(30);
          m_connected = true;
          active_category = m_active_category;
          if (!active_category.empty()) {
            m_ready_category.clear();
            m_ready_replay_ids.clear();
            m_pending_category = active_category;
          }
          selection_revision = ++m_selection_revision;
          m_status = active_category.empty()
                         ? fmt::format("Connected - {} replays; select a mission", m_replays.size())
                         : fmt::format("Connected - {} total replays", m_replays.size());
        }
        resolve_and_download(active_category, false, selection_revision);
      } catch (const std::exception& e) {
        set_connected(false, fmt::format("Invalid replay-server state: {}", e.what()));
      }
    });
  }

  std::string player_name() {
    bool refresh_identity_now = false;
    std::string player_name;
    {
      std::lock_guard lock(m_state_mutex);
      const auto now = std::chrono::steady_clock::now();
      if (!m_identity_refresh_pending && now >= m_next_identity_refresh) {
        m_identity_refresh_pending = true;
        m_next_identity_refresh = now + std::chrono::seconds(30);
        refresh_identity_now = true;
      }
      player_name = m_player_name;
    }
    if (refresh_identity_now) {
      refresh_identity();
    }
    return player_name;
  }

  bool ping_unknown_player() {
    {
      std::lock_guard lock(m_state_mutex);
      if (!m_player_name.empty() || m_unknown_ping_pending) {
        return false;
      }
      m_unknown_ping_pending = true;
      m_status = "Sending unknown-player identity ping...";
    }
    enqueue([this]() {
      const auto body = json{{"player_id", persistent_player_id()}}.dump();
      const auto response = request("POST", "/api/unknown-player-ping", body);
      std::lock_guard lock(m_state_mutex);
      m_unknown_ping_pending = false;
      m_status = response.ok
                     ? fmt::format("Identity ping sent for Player ID {}", persistent_player_id())
                     : fmt::format("Identity ping failed: {}", response.error);
    });
    return true;
  }

  void refresh_point_leaderboard() {
    int revision = 0;
    int mode_index = 0;
    int group_index = 0;
    {
      std::lock_guard lock(m_state_mutex);
      revision = ++m_point_leaderboard_revision;
      mode_index = m_point_leaderboard_mode_index;
      group_index = m_point_leaderboard_group_index;
      m_point_leaderboard_pending = true;
      m_point_leaderboard_state = 0;
      m_point_leaderboard_status = "Loading JakMods point standings...";
    }
    enqueue([this, revision, mode_index, group_index]() {
      const auto response = request(
          "GET", fmt::format("/api/point-leaderboard?mode={}&group={}",
                             kPointLeaderboardModes.at(mode_index).id,
                             kPointLeaderboardGroups.at(group_index).id));
      if (!response.ok) {
        std::lock_guard lock(m_state_mutex);
        if (revision == m_point_leaderboard_revision) {
          m_point_leaderboard_pending = false;
          m_point_leaderboard_state = -1;
          m_point_leaderboard_status =
              fmt::format("Could not load point standings: {}", response.error);
        }
        return;
      }
      const auto parsed = safe_parse_json(response.body);
      if (!parsed || !parsed->contains("entries") || !parsed->at("entries").is_array()) {
        std::lock_guard lock(m_state_mutex);
        if (revision == m_point_leaderboard_revision) {
          m_point_leaderboard_pending = false;
          m_point_leaderboard_state = -1;
          m_point_leaderboard_status = "Point leaderboard returned invalid JSON";
        }
        return;
      }
      std::vector<PointLeaderboardEntry> entries;
      try {
        for (const auto& item : parsed->at("entries")) {
          entries.push_back({item.value("rank", 0), item.value("rank_label", ""),
                             item.value("runner", "Unknown runner"), item.value("points", 0),
                             item.value("missions_run", 0), item.value("missions_total", 0),
                             item.value("tied_wrs", 0), item.value("untied_wrs", 0)});
        }
      } catch (const std::exception& error) {
        std::lock_guard lock(m_state_mutex);
        if (revision == m_point_leaderboard_revision) {
          m_point_leaderboard_pending = false;
          m_point_leaderboard_state = -1;
          m_point_leaderboard_status =
              fmt::format("Could not parse point standings: {}", error.what());
        }
        return;
      }
      std::lock_guard lock(m_state_mutex);
      if (revision != m_point_leaderboard_revision) {
        return;
      }
      m_point_leaderboard_entries = std::move(entries);
      m_point_leaderboard_pending = false;
      m_point_leaderboard_state = 1;
      m_point_leaderboard_status =
          fmt::format("{} / {} - {} runner{}", kPointLeaderboardModes.at(mode_index).label,
                      kPointLeaderboardGroups.at(group_index).label,
                      m_point_leaderboard_entries.size(),
                      m_point_leaderboard_entries.size() == 1 ? "" : "s");
    });
  }

  bool select_point_leaderboard(int mode_index, int group_index) {
    if (mode_index < 0 || mode_index >= static_cast<int>(kPointLeaderboardModes.size()) ||
        group_index < 0 || group_index >= static_cast<int>(kPointLeaderboardGroups.size())) {
      return false;
    }
    {
      std::lock_guard lock(m_state_mutex);
      m_point_leaderboard_mode_index = mode_index;
      m_point_leaderboard_group_index = group_index;
      m_point_leaderboard_entries.clear();
    }
    refresh_point_leaderboard();
    return true;
  }

  int point_leaderboard_state() {
    std::lock_guard lock(m_state_mutex);
    return m_point_leaderboard_state;
  }

  int point_leaderboard_count() {
    std::lock_guard lock(m_state_mutex);
    return static_cast<int>(m_point_leaderboard_entries.size());
  }

  std::string point_leaderboard_label(int index) {
    std::lock_guard lock(m_state_mutex);
    if (index < 0 || index >= static_cast<int>(m_point_leaderboard_entries.size())) {
      return {};
    }
    const auto& entry = m_point_leaderboard_entries.at(index);
    return fmt::format("{}  {}  {} pts  {}/{}  WR {}+{}", entry.rank_label, entry.runner,
                       entry.points, entry.missions_run, entry.missions_total, entry.tied_wrs,
                       entry.untied_wrs);
  }

  int point_leaderboard_value(int index, int field) {
    std::lock_guard lock(m_state_mutex);
    if (index < 0 || index >= static_cast<int>(m_point_leaderboard_entries.size())) {
      return 0;
    }
    const auto& entry = m_point_leaderboard_entries.at(index);
    switch (field) {
      case 0:
        return entry.rank;
      case 1:
        return entry.points;
      case 2:
        return entry.missions_run;
      case 3:
        return entry.missions_total;
      case 4:
        return entry.tied_wrs;
      case 5:
        return entry.untied_wrs;
      default:
        return 0;
    }
  }

  std::string point_leaderboard_text(int index, int field) {
    std::lock_guard lock(m_state_mutex);
    if (index < 0 || index >= static_cast<int>(m_point_leaderboard_entries.size())) {
      return {};
    }
    const auto& entry = m_point_leaderboard_entries.at(index);
    switch (field) {
      case 0:
        return entry.rank_label;
      case 1:
        return entry.runner;
      default:
        return {};
    }
  }

  int point_leaderboard_mode_index() {
    std::lock_guard lock(m_state_mutex);
    return m_point_leaderboard_mode_index;
  }

  bool set_point_leaderboard_mode(int index) {
    if (index < 0 || index >= static_cast<int>(kPointLeaderboardModes.size())) {
      return false;
    }
    {
      std::lock_guard lock(m_state_mutex);
      m_point_leaderboard_mode_index = index;
      m_point_leaderboard_entries.clear();
    }
    refresh_point_leaderboard();
    return true;
  }

  int point_leaderboard_group_index() {
    std::lock_guard lock(m_state_mutex);
    return m_point_leaderboard_group_index;
  }

  bool set_point_leaderboard_group(int index) {
    if (index < 0 || index >= static_cast<int>(kPointLeaderboardGroups.size())) {
      return false;
    }
    {
      std::lock_guard lock(m_state_mutex);
      m_point_leaderboard_group_index = index;
      m_point_leaderboard_entries.clear();
    }
    refresh_point_leaderboard();
    return true;
  }

  std::string point_leaderboard_status() {
    std::lock_guard lock(m_state_mutex);
    return m_point_leaderboard_status;
  }

  void publish(const std::string& replay_path, ReplayLevelResolver level_resolver) {
    // The GOAL caller can be running on a small process stack during mission
    // finalization. Keep this bridge deliberately tiny and do every file/JSON
    // operation on the native worker thread.
    set_status("Preparing recorded replay for upload...");
    enqueue([this, replay_path, level_resolver = std::move(level_resolver)]() {
      try {
        const auto replay = safe_parse_json(file_util::read_text_file(replay_path));
        if (!replay || !replay->is_object()) {
          set_status("Could not parse the recorded replay JSON");
          return;
        }

        const auto category = replay->value("category", "");
        const auto src_level_id = level_resolver(category);
        if (!src_level_id) {
          set_status(fmt::format("No Speedrun.com mission mapping for {}", category));
          return;
        }
        const auto time_seconds = replay->value("time_seconds", 0.f);
        if (time_seconds <= 0.f) {
          set_status("Recorded replay has an invalid completion time");
          return;
        }
        const auto is_personal_best = replay->value("is_personal_best", false);
        const auto completed = replay->value("completed", true);
        const auto src_category_id =
            replay->value("percent_warped", 0) == 0 ? "rkl7n8qd" : "7dgw7742";
        const auto vehicle_name = replay->value("vehicle_name", "N/A");
        json envelope = {{"game", version_to_game_name(g_game_version)},
                         {"category", category},
                         {"player_id", persistent_player_id()},
                         {"time_seconds", time_seconds},
                         {"src_level_id", *src_level_id},
                         {"src_category_id", src_category_id},
                         {"src_variable_labels", {{"Wasteland Vehicle", vehicle_name}}},
                         {"is_personal_best", is_personal_best},
                         {"completed", completed},
                         {"replay", *replay}};
        set_status(is_personal_best ? "Uploading new personal-best replay..."
                   : completed      ? "Uploading completed replay..."
                                    : "Uploading unfinished attempt...");
        auto response = request("POST", "/api/replays", envelope.dump());
        if (!response.ok) {
          set_connected(false, fmt::format("Replay upload failed: {}", response.error));
          return;
        }
        {
          std::lock_guard lock(m_state_mutex);
          if (m_active_category == category) {
            m_ready_category.clear();
            m_ready_replay_ids.clear();
            m_pending_category.clear();
            ++m_selection_revision;
          }
        }
        set_status("Replay uploaded");
        refresh();
      } catch (const std::exception& e) {
        set_status(fmt::format("Replay upload failed: {}", e.what()));
      }
    });
  }

  int prepare_selected(const std::string& category) {
    int selection_revision = 0;
    bool start_initial_refresh = false;
    {
      std::lock_guard lock(m_state_mutex);
      if (!m_refresh_started) {
        // The replay UI used to be the only automatic refresh path. Start it
        // here as well so the first mission attempt has current runner names
        // and server state even when no replay/progress menu was opened.
        m_refresh_started = true;
        start_initial_refresh = true;
      }
      if (m_active_category == category && m_ready_category == category && m_ready_generation > 0) {
        return m_ready_generation;
      }
      if (m_active_category == category && m_pending_category == category) {
        return 0;
      }
      m_active_category = category;
      m_ready_category.clear();
      m_ready_replay_ids.clear();
      m_pending_category = category;
      selection_revision = ++m_selection_revision;
    }
    if (start_initial_refresh) {
      refresh();
    }
    enqueue([this, category, selection_revision]() {
      resolve_and_download(category, false, selection_revision);
    });
    return 0;
  }

  int selected_count() {
    std::lock_guard lock(m_state_mutex);
    return m_ready_category == m_active_category ? static_cast<int>(m_ready_replay_ids.size()) : 0;
  }

  int mission_replay_count() {
    std::lock_guard lock(m_state_mutex);
    return static_cast<int>(
        std::count_if(m_replays.begin(), m_replays.end(),
                      [this](const auto& replay) { return replay.category == m_active_category; }));
  }

  std::string mission_replay_label(int index) {
    std::lock_guard lock(m_state_mutex);
    const auto* replay = mission_replay_at_index(index);
    if (!replay) {
      return {};
    }
    return replay->completed
               ? fmt::format("{}  ({:.3f}s)", runner_name(*replay), replay->time_seconds)
               : fmt::format("{}  ({:.3f}s, unfinished)", runner_name(*replay),
                             replay->time_seconds);
  }

  bool mission_replay_selected(int index) {
    std::lock_guard lock(m_state_mutex);
    const auto* replay = mission_replay_at_index(index);
    return replay && std::find(m_selected_replay_ids.begin(), m_selected_replay_ids.end(),
                               replay->id) != m_selected_replay_ids.end();
  }

  bool toggle_mission_replay(int index) {
    std::string replay_id;
    {
      std::lock_guard lock(m_state_mutex);
      const auto* replay = mission_replay_at_index(index);
      if (!replay) {
        return false;
      }
      replay_id = replay->id;
    }
    toggle_replay(replay_id);
    return true;
  }

  int mode_count() {
    std::lock_guard lock(m_state_mutex);
    return static_cast<int>(m_modes.size());
  }

  std::string mode_label(int index) {
    std::lock_guard lock(m_state_mutex);
    if (index < 0 || index >= static_cast<int>(m_modes.size())) {
      return {};
    }
    return m_modes.at(index).label;
  }

  bool mode_selected(int index) {
    std::lock_guard lock(m_state_mutex);
    return index >= 0 && index < static_cast<int>(m_modes.size()) &&
           m_modes.at(index).id == m_replay_mode;
  }

  bool custom_mode_selected() {
    std::lock_guard lock(m_state_mutex);
    return m_replay_mode == "custom";
  }

  bool set_mode(int index) {
    std::string mode_id;
    std::string category;
    int selection_revision = 0;
    {
      std::lock_guard lock(m_state_mutex);
      if (index < 0 || index >= static_cast<int>(m_modes.size())) {
        return false;
      }
      mode_id = m_modes.at(index).id;
      m_replay_mode = mode_id;
      category = m_active_category;
      m_ready_replay_ids.clear();
      m_ready_category.clear();
      m_pending_category = category;
      selection_revision = ++m_selection_revision;
    }
    enqueue([this, mode_id, category, selection_revision]() {
      json body = {{"replay_mode", mode_id}};
      auto response = request(
          "PATCH", fmt::format("/api/player-settings/{}", persistent_player_id()), body.dump());
      if (!response.ok) {
        clear_pending_selection(category, selection_revision);
        set_status(fmt::format("Could not save ghost mode: {}", response.error));
        return;
      }
      resolve_and_download(category, true, selection_revision);
    });
    return true;
  }

  std::string ready_replay_name(int index) {
    std::lock_guard lock(m_state_mutex);
    if (index < 0 || index >= static_cast<int>(m_ready_replay_ids.size()) ||
        m_ready_category != m_active_category) {
      return {};
    }
    const auto found = std::find_if(m_replays.begin(), m_replays.end(), [&](const auto& replay) {
      return replay.id == m_ready_replay_ids.at(index);
    });
    return found == m_replays.end() ? std::string{} : runner_name(*found);
  }

  std::string status() {
    std::lock_guard lock(m_state_mutex);
    return m_status;
  }

  void toggle_replay(const std::string& replay_id) {
    std::vector<std::string> selected_replays;
    std::string category;
    int selection_revision = 0;
    {
      std::lock_guard lock(m_state_mutex);
      const auto selected =
          std::find(m_selected_replay_ids.begin(), m_selected_replay_ids.end(), replay_id);
      if (selected != m_selected_replay_ids.end()) {
        m_selected_replay_ids.erase(selected);
      } else {
        const auto selected_for_mission = std::count_if(
            m_selected_replay_ids.begin(), m_selected_replay_ids.end(), [this](const auto& id) {
              const auto replay = std::find_if(m_replays.begin(), m_replays.end(),
                                               [&id](const auto& item) { return item.id == id; });
              return replay != m_replays.end() && replay->category == m_active_category;
            });
        if (selected_for_mission >= kMaxSelectedReplays) {
          m_status = fmt::format("Select at most {} replays per mission", kMaxSelectedReplays);
          return;
        }
        m_selected_replay_ids.push_back(replay_id);
      }
      selected_replays = m_selected_replay_ids;
      category = m_active_category;
      m_replay_mode = "custom";
      selection_revision = ++m_selection_revision;
      m_ready_replay_ids.clear();
      m_ready_category.clear();
      m_pending_category = category;
    }
    enqueue([this, selected_replays = std::move(selected_replays), category, selection_revision]() {
      json body = {{"selected_replay_ids", selected_replays}, {"replay_mode", "custom"}};
      auto response = request(
          "PATCH", fmt::format("/api/player-settings/{}", persistent_player_id()), body.dump());
      if (!response.ok) {
        clear_pending_selection(category, selection_revision);
        set_status(fmt::format("Could not save replay selections: {}", response.error));
        return;
      }
      resolve_and_download(category, true, selection_revision);
    });
  }

  void draw(bool* open) {
    bool do_refresh = false;
    std::vector<ReplayInfo> replays;
    std::vector<RunnerInfo> runners;
    std::vector<ReplayModeInfo> modes;
    std::vector<std::string> selected_replays;
    std::string active_category;
    std::string replay_mode;
    std::string status;
    bool connected = false;
    {
      std::lock_guard lock(m_state_mutex);
      if (!m_refresh_started) {
        m_refresh_started = true;
        do_refresh = true;
      }
      replays = m_replays;
      runners = m_runners;
      modes = m_modes;
      selected_replays = m_selected_replay_ids;
      active_category = m_active_category;
      replay_mode = m_replay_mode;
      status = m_status;
      connected = m_connected;
    }
    if (do_refresh) {
      refresh();
    }
    if (!ImGui::Begin("Replay Server", open)) {
      ImGui::End();
      return;
    }
    ImGui::TextColored(
        connected ? ImVec4(0.35f, 0.85f, 0.65f, 1.f) : ImVec4(1.f, 0.55f, 0.45f, 1.f), "%s",
        status.c_str());
    if (ImGui::Button("Refresh")) {
      refresh();
    }
    ImGui::SameLine();
    if (ImGui::Button("Copy dashboard URL")) {
      ImGui::SetClipboardText(server_config_snapshot().url.c_str());
    }
    ImGui::SameLine();
    if (ImGui::Button("Copy player ID")) {
      ImGui::SetClipboardText(persistent_player_id().c_str());
    }
    ImGui::Text("Player ID: %s", persistent_player_id().c_str());
    ImGui::Separator();

    if (ImGui::CollapsingHeader("Server connection")) {
      ImGui::InputText("Server URL", m_server_url.data(), m_server_url.size());
      ImGui::InputText("Game token", m_game_token.data(), m_game_token.size(),
                       ImGuiInputTextFlags_Password);
      if (ImGui::Button("Save connection")) {
        try {
          save_server_config(m_server_url.data(), m_game_token.data());
          set_connected(false, "Replay server settings saved; refreshing...");
          refresh();
        } catch (const std::exception& error) {
          set_status(fmt::format("Could not save replay server settings: {}", error.what()));
        }
      }
      ImGui::TextWrapped(
          "Release builds connect to the public SparkedHost replay server automatically. "
          "These fields are only needed to override that connection. The admin token is never "
          "entered in-game.");
      ImGui::Separator();
    }

    ImGui::Text("Mission: %s", active_category.empty() ? "select one in the Speedrun Menu"
                                                       : active_category.c_str());
    const auto active_mode = std::find_if(modes.begin(), modes.end(),
                                          [&](const auto& mode) { return mode.id == replay_mode; });
    const char* mode_preview =
        active_mode == modes.end() ? "Unknown mode" : active_mode->label.c_str();
    if (ImGui::BeginCombo("Ghost mode", mode_preview)) {
      for (size_t index = 0; index < modes.size(); ++index) {
        if (ImGui::Selectable(modes.at(index).label.c_str(), modes.at(index).id == replay_mode)) {
          set_mode(static_cast<int>(index));
        }
      }
      ImGui::EndCombo();
    }
    if (active_mode != modes.end()) {
      ImGui::TextWrapped("%s", active_mode->description.c_str());
    }
    ImGui::TextUnformatted(replay_mode == "custom" ? "Race against (click to toggle):"
                                                   : "Available mission replays:");
    int mission_replay_count = 0;
    if (ImGui::BeginChild("Mission replays", ImVec2(0.f, 180.f), true)) {
      for (const auto& replay : replays) {
        if (replay.category != active_category) {
          continue;
        }
        ++mission_replay_count;
        const bool selected = std::find(selected_replays.begin(), selected_replays.end(),
                                        replay.id) != selected_replays.end();
        auto runner_name = replay.display_name;
        if (!replay.src_runner_id.empty()) {
          const auto runner =
              std::find_if(runners.begin(), runners.end(),
                           [&replay](const auto& item) { return item.id == replay.src_runner_id; });
          if (runner != runners.end()) {
            runner_name = runner->display_name;
          }
        }
        const auto label =
            replay.completed
                ? fmt::format("{}  ({:.3f}s)##{}", runner_name, replay.time_seconds, replay.id)
                : fmt::format("{}  ({:.3f}s, unfinished)##{}", runner_name, replay.time_seconds,
                              replay.id);
        ImGui::PushStyleColor(ImGuiCol_Text, selected ? ImVec4(0.25f, 1.f, 0.35f, 1.f)
                                                      : ImVec4(1.f, 0.25f, 0.25f, 1.f));
        if (ImGui::Selectable(label.c_str(), selected,
                              replay_mode != "custom" ? ImGuiSelectableFlags_Disabled : 0)) {
          toggle_replay(replay.id);
        }
        ImGui::PopStyleColor();
      }
      if (!active_category.empty() && mission_replay_count == 0) {
        ImGui::TextDisabled("No server replays for this mission. Press Refresh to check again.");
      }
    }
    ImGui::EndChild();

    ImGui::TextWrapped(
        "This permanent Player ID is sent with every replay. An admin maps it to your "
        "Speedrun.com runner once in the dashboard; existing and future PBs then use that "
        "runner. Configure the moderator key and reload runners there. "
        "Replay names are red when inactive and green when selected. All selected replays for "
        "the current mission race together on the next attempt.");
    ImGui::End();
  }

 private:
  void refresh_identity() {
    enqueue([this]() {
      const auto response =
          request("GET", fmt::format("/api/state?player_id={}", persistent_player_id()));
      if (!response.ok) {
        std::lock_guard lock(m_state_mutex);
        m_identity_refresh_pending = false;
        return;
      }
      const auto parsed = safe_parse_json(response.body);
      if (!parsed) {
        std::lock_guard lock(m_state_mutex);
        m_identity_refresh_pending = false;
        return;
      }
      const auto player_name = mapped_player_name(*parsed);
      std::lock_guard lock(m_state_mutex);
      m_player_name = player_name;
      m_identity_refresh_pending = false;
    });
  }

  const ReplayInfo* mission_replay_at_index(int index) const {
    if (index < 0) {
      return nullptr;
    }
    for (const auto& replay : m_replays) {
      if (replay.category == m_active_category && index-- == 0) {
        return &replay;
      }
    }
    return nullptr;
  }

  std::string runner_name(const ReplayInfo& replay) const {
    if (!replay.src_runner_name.empty()) {
      return replay.src_runner_name;
    }
    if (!replay.src_runner_id.empty()) {
      const auto runner = std::find_if(m_runners.begin(), m_runners.end(), [&](const auto& item) {
        return item.id == replay.src_runner_id;
      });
      if (runner != m_runners.end()) {
        return runner->display_name;
      }
    }
    return replay.display_name;
  }

  void worker_loop() {
    while (true) {
      std::function<void()> job;
      {
        std::unique_lock lock(m_job_mutex);
        m_job_cv.wait(lock, [this]() { return m_stopping || !m_jobs.empty(); });
        if (m_stopping && m_jobs.empty()) {
          return;
        }
        job = std::move(m_jobs.front());
        m_jobs.pop();
      }
      try {
        job();
      } catch (const std::exception& error) {
        const auto status = fmt::format("Replay background job failed: {}", error.what());
        lg::error("{}", status);
        set_connected(false, status);
      } catch (...) {
        constexpr auto* status = "Replay background job failed with an unknown exception";
        lg::error("{}", status);
        set_connected(false, status);
      }
    }
  }

  void resolve_and_download(const std::string& category, bool announce, int selection_revision) {
    if (category.empty()) {
      return;
    }
    json body = {{"category", category}, {"player_id", persistent_player_id()}};
    auto response = request("POST", "/api/replay-selection", body.dump());
    if (!response.ok) {
      clear_pending_selection(category, selection_revision);
      set_status(fmt::format("Could not resolve ghost mode: {}", response.error));
      return;
    }
    const auto parsed = safe_parse_json(response.body);
    if (!parsed || !parsed->contains("replays") || !parsed->at("replays").is_array()) {
      clear_pending_selection(category, selection_revision);
      set_status("Replay server returned an invalid ghost selection");
      return;
    }
    std::vector<ReplayInfo> selected;
    for (const auto& item : parsed->at("replays")) {
      const auto replay_id = item.value("id", "");
      if (!replay_id.empty() && item.value("category", "") == category &&
          selected.size() < static_cast<size_t>(kMaxSelectedReplays)) {
        selected.push_back({replay_id, item.value("display_name", "Unnamed replay"),
                            item.value("category", ""), item.value("src_status", ""),
                            item.value("src_runner_id", ""), item.value("src_runner_name", ""),
                            item.value("completed", true), item.value("time_seconds", 0.f)});
      }
    }
    {
      std::lock_guard lock(m_state_mutex);
      for (const auto& replay : selected) {
        const auto found = std::find_if(m_replays.begin(), m_replays.end(),
                                        [&](const auto& item) { return item.id == replay.id; });
        if (found == m_replays.end()) {
          m_replays.push_back(replay);
        } else {
          *found = replay;
        }
      }
    }
    download_selected_pack(selected, category, announce, selection_revision);
  }

  void download_selected_pack(const std::vector<ReplayInfo>& selected,
                              const std::string& category,
                              bool announce,
                              int selection_revision) {
    if (category.empty()) {
      return;
    }
    if (announce) {
      set_status(selected.empty()
                     ? "Replay opponents disabled"
                     : fmt::format("Downloading {} selected replays...", selected.size()));
    }
    try {
      file_util::create_dir_if_needed("ghost");
      std::vector<std::string> replay_payloads;
      replay_payloads.reserve(selected.size());
      for (size_t index = 0; index < selected.size(); ++index) {
        auto response =
            request("GET", fmt::format("/api/replays/{}/download", selected.at(index).id));
        if (!response.ok) {
          clear_pending_selection(category, selection_revision);
          set_status(fmt::format("Could not download {}: {}", selected.at(index).display_name,
                                 response.error));
          return;
        }
        replay_payloads.push_back(std::move(response.body));
      }
      std::lock_guard lock(m_state_mutex);
      if (selection_revision != m_selection_revision || category != m_active_category) {
        return;
      }
      // Commit the downloaded pack only after confirming it is still current.
      // Previously, a superseded background job could overwrite these files
      // before its revision was rejected, pairing current IDs/names with stale
      // replay data and making first-attempt playback intermittent.
      for (size_t index = 0; index < replay_payloads.size(); ++index) {
        file_util::write_text_file(fmt::format("ghost/selected-replay-{}.json", index),
                                   replay_payloads.at(index));
      }
      m_pending_category.clear();
      m_ready_replay_ids.clear();
      for (const auto& replay : selected) {
        m_ready_replay_ids.push_back(replay.id);
      }
      m_ready_category = category;
      ++m_ready_generation;
      if (m_ready_generation <= 0) {
        m_ready_generation = 1;
      }
      m_status = selected.empty()
                     ? fmt::format("Ready - no opponents selected for {}", category)
                     : fmt::format("Ready - {} replay{} selected for {}", selected.size(),
                                   selected.size() == 1 ? "" : "s", category);
    } catch (const std::exception& e) {
      clear_pending_selection(category, selection_revision);
      set_status(fmt::format("Could not save selected replays: {}", e.what()));
    }
  }

  void clear_pending_selection(const std::string& category, int selection_revision) {
    std::lock_guard lock(m_state_mutex);
    if (selection_revision == m_selection_revision && m_pending_category == category) {
      m_pending_category.clear();
    }
  }

  void set_connected(bool connected, const std::string& status) {
    std::lock_guard lock(m_state_mutex);
    m_connected = connected;
    m_status = status;
  }

  void set_status(const std::string& status) {
    std::lock_guard lock(m_state_mutex);
    m_status = status;
  }

  std::mutex m_job_mutex;
  std::condition_variable m_job_cv;
  std::queue<std::function<void()>> m_jobs;
  bool m_stopping = false;
  std::thread m_worker;

  std::array<char, 512> m_server_url{};
  std::array<char, 256> m_game_token{};

  std::mutex m_state_mutex;
  std::vector<ReplayInfo> m_replays;
  std::vector<RunnerInfo> m_runners;
  std::vector<ReplayModeInfo> m_modes = fallback_modes();
  std::vector<std::string> m_selected_replay_ids;
  std::vector<std::string> m_ready_replay_ids;
  std::string m_replay_mode = "default";
  std::string m_active_category;
  std::string m_ready_category;
  std::string m_pending_category;
  std::string m_status = "Replay server has not been contacted";
  std::string m_player_name;
  std::chrono::steady_clock::time_point m_next_identity_refresh{};
  int m_ready_generation = 0;
  int m_selection_revision = 0;
  bool m_connected = false;
  bool m_refresh_started = false;
  bool m_identity_refresh_pending = false;
  bool m_unknown_ping_pending = false;
  std::vector<PointLeaderboardEntry> m_point_leaderboard_entries;
  std::string m_point_leaderboard_status = "Point standings have not been loaded";
  int m_point_leaderboard_mode_index = 1;
  int m_point_leaderboard_group_index = 0;
  int m_point_leaderboard_revision = 0;
  bool m_point_leaderboard_pending = false;
  int m_point_leaderboard_state = -1;
};

Client& client() {
  static Client instance;
  return instance;
}

}  // namespace

void refresh() {
  client().refresh();
}

void publish(const std::string& replay_path, ReplayLevelResolver level_resolver) {
  client().publish(replay_path, std::move(level_resolver));
}

int prepare_selected(const std::string& category) {
  return client().prepare_selected(category);
}

int selected_count() {
  return client().selected_count();
}

int mission_replay_count() {
  return client().mission_replay_count();
}

std::string mission_replay_label(int index) {
  return client().mission_replay_label(index);
}

bool mission_replay_selected(int index) {
  return client().mission_replay_selected(index);
}

bool toggle_mission_replay(int index) {
  return client().toggle_mission_replay(index);
}

int mode_count() {
  return client().mode_count();
}

std::string mode_label(int index) {
  return client().mode_label(index);
}

bool mode_selected(int index) {
  return client().mode_selected(index);
}

bool custom_mode_selected() {
  return client().custom_mode_selected();
}

bool set_mode(int index) {
  return client().set_mode(index);
}

std::string ready_replay_name(int index) {
  return client().ready_replay_name(index);
}

std::string status() {
  return client().status();
}

std::string player_id() {
  return persistent_player_id();
}

std::string player_name() {
  return client().player_name();
}

bool ping_unknown_player() {
  return client().ping_unknown_player();
}

void refresh_point_leaderboard() {
  client().refresh_point_leaderboard();
}

bool select_point_leaderboard(int mode_index, int group_index) {
  return client().select_point_leaderboard(mode_index, group_index);
}

int point_leaderboard_state() {
  return client().point_leaderboard_state();
}

int point_leaderboard_count() {
  return client().point_leaderboard_count();
}

std::string point_leaderboard_label(int index) {
  return client().point_leaderboard_label(index);
}

int point_leaderboard_value(int index, int field) {
  return client().point_leaderboard_value(index, field);
}

std::string point_leaderboard_text(int index, int field) {
  return client().point_leaderboard_text(index, field);
}

int point_leaderboard_mode_count() {
  return static_cast<int>(kPointLeaderboardModes.size());
}

std::string point_leaderboard_mode_label(int index) {
  return index >= 0 && index < static_cast<int>(kPointLeaderboardModes.size())
             ? kPointLeaderboardModes.at(index).label
             : std::string{};
}

int point_leaderboard_mode_index() {
  return client().point_leaderboard_mode_index();
}

bool set_point_leaderboard_mode(int index) {
  return client().set_point_leaderboard_mode(index);
}

int point_leaderboard_group_count() {
  return static_cast<int>(kPointLeaderboardGroups.size());
}

std::string point_leaderboard_group_label(int index) {
  return index >= 0 && index < static_cast<int>(kPointLeaderboardGroups.size())
             ? kPointLeaderboardGroups.at(index).label
             : std::string{};
}

int point_leaderboard_group_index() {
  return client().point_leaderboard_group_index();
}

bool set_point_leaderboard_group(int index) {
  return client().set_point_leaderboard_group(index);
}

std::string point_leaderboard_status() {
  return client().point_leaderboard_status();
}

void draw_window(bool* open) {
  client().draw(open);
}

}  // namespace replay_client
