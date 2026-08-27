#include "replay_client.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <condition_variable>
#include <cstring>
#include <functional>
#include <mutex>
#include <optional>
#include <queue>
#include <random>
#include <thread>
#include <utility>
#include <vector>

#include "common/log/log.h"
#include "common/util/FileUtil.h"
#include "common/util/json_util.h"

#include "curl/curl.h"
#include "fmt/format.h"
#include "game/runtime.h"
#include "third-party/imgui/imgui.h"

namespace replay_client {
namespace {

constexpr const char* kServerUrl = "http://127.0.0.1:7878";
constexpr int kMaxSelectedReplays = 33;

bool valid_player_id(const std::string& value) {
  return value.size() == 32 &&
         std::all_of(value.begin(), value.end(), [](unsigned char character) {
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
        stored.erase(std::remove_if(stored.begin(), stored.end(), [](unsigned char character) {
                       return std::isspace(character) != 0;
                     }),
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
  float time_seconds = 0.f;
};

struct RunnerInfo {
  std::string id;
  std::string display_name;
};

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
  const auto url = fmt::format("{}{}", kServerUrl, path);
  curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
  curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, 10000L);
  curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, 1000L);
  curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
  curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback);
  curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response.body);
  struct curl_slist* headers = nullptr;
  if (method != "GET") {
    curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, method.c_str());
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.c_str());
    curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(body.size()));
    headers = curl_slist_append(headers, "Content-Type: application/json");
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
  Client() : m_worker([this]() { worker_loop(); }) {}

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
    set_status("Refreshing replay server...");
    enqueue([this]() {
      auto response = request("GET", "/api/state");
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
      try {
        for (const auto& item : parsed->at("replays")) {
          replays.push_back({item.value("id", ""), item.value("display_name", "Unnamed replay"),
                             item.value("category", ""), item.value("src_status", ""),
                             item.value("src_runner_id", ""),
                             item.value("time_seconds", 0.f)});
        }
        for (const auto& item : parsed->at("runners")) {
          runners.push_back({item.value("id", ""), item.value("display_name", "Unknown runner")});
        }
        const auto& settings = parsed->at("settings");
        std::vector<std::string> selected_replays;
        if (settings.contains("selected_replay_ids") && settings.at("selected_replay_ids").is_array()) {
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
          m_selected_replay_ids = selected_replays;
          m_connected = true;
          active_category = m_active_category;
          selection_revision = ++m_selection_revision;
          m_status = active_category.empty()
                         ? fmt::format("Connected - {} replays; select a mission", m_replays.size())
                         : fmt::format("Connected - {} total replays", m_replays.size());
        }
        download_selected_pack(selected_replays, active_category, false, selection_revision);
      } catch (const std::exception& e) {
        set_connected(false, fmt::format("Invalid replay-server state: {}", e.what()));
      }
    });
  }

  void publish(const std::string& replay_path,
               const std::string& category,
               float time_seconds,
               const std::string& src_level_id,
               const std::string& src_category_id,
               const std::string& vehicle_name,
               bool is_personal_best) {
    try {
      // Snapshot the file before queuing the upload. A later PB may reuse the same path.
      const auto replay = safe_parse_json(file_util::read_text_file(replay_path));
      if (!replay) {
        set_status("Could not parse the recorded replay JSON");
        return;
      }
      json envelope = {{"category", category},
                       {"player_id", persistent_player_id()},
                       {"time_seconds", time_seconds},
                       {"src_level_id", src_level_id},
                       {"src_category_id", src_category_id},
                       {"src_variable_labels", {{"Wasteland Vehicle", vehicle_name}}},
                       {"is_personal_best", is_personal_best},
                       {"replay", *replay}};
      auto upload_body = envelope.dump();
      set_status(is_personal_best ? "Uploading new personal-best replay..."
                                  : "Uploading completed replay...");
      enqueue([this, upload_body = std::move(upload_body)]() {
        try {
          auto response = request("POST", "/api/replays", upload_body);
          if (!response.ok) {
            set_connected(false, fmt::format("Replay upload failed: {}", response.error));
            return;
          }
          set_status("Completed replay uploaded");
          refresh();
        } catch (const std::exception& e) {
          set_status(fmt::format("Replay upload failed: {}", e.what()));
        }
      });
    } catch (const std::exception& e) {
      set_status(fmt::format("Could not read the recorded replay: {}", e.what()));
    }
  }

  int prepare_selected(const std::string& category) {
    std::vector<std::string> selected_replays;
    int selection_revision = 0;
    {
      std::lock_guard lock(m_state_mutex);
      if (m_active_category == category && m_ready_category == category &&
          m_ready_generation > 0) {
        return m_ready_generation;
      }
      m_active_category = category;
      m_ready_category.clear();
      m_ready_replay_ids.clear();
      selected_replays = m_selected_replay_ids;
      selection_revision = ++m_selection_revision;
    }
    enqueue([this, selected_replays = std::move(selected_replays), category, selection_revision]() {
      download_selected_pack(selected_replays, category, false, selection_revision);
    });
    return 0;
  }

  int selected_count() {
    std::lock_guard lock(m_state_mutex);
    return m_ready_category == m_active_category ? static_cast<int>(m_ready_replay_ids.size()) : 0;
  }

  int mission_replay_count() {
    std::lock_guard lock(m_state_mutex);
    return static_cast<int>(std::count_if(m_replays.begin(), m_replays.end(), [this](const auto& replay) {
      return replay.category == m_active_category;
    }));
  }

  std::string mission_replay_label(int index) {
    std::lock_guard lock(m_state_mutex);
    const auto* replay = mission_replay_at_index(index);
    if (!replay) {
      return {};
    }
    return fmt::format("{}  ({:.3f}s)", runner_name(*replay), replay->time_seconds);
  }

  bool mission_replay_selected(int index) {
    std::lock_guard lock(m_state_mutex);
    const auto* replay = mission_replay_at_index(index);
    return replay && std::find(m_selected_replay_ids.begin(), m_selected_replay_ids.end(), replay->id) !=
                         m_selected_replay_ids.end();
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
      const auto selected = std::find(m_selected_replay_ids.begin(), m_selected_replay_ids.end(),
                                      replay_id);
      if (selected != m_selected_replay_ids.end()) {
        m_selected_replay_ids.erase(selected);
      } else {
        const auto selected_for_mission =
            std::count_if(m_selected_replay_ids.begin(), m_selected_replay_ids.end(),
                          [this](const auto& id) {
                            const auto replay = std::find_if(
                                m_replays.begin(), m_replays.end(),
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
      selection_revision = ++m_selection_revision;
      m_ready_replay_ids.clear();
      m_ready_category.clear();
    }
    enqueue([this, selected_replays = std::move(selected_replays), category,
             selection_revision]() {
      json body = {{"selected_replay_ids", selected_replays}};
      auto response = request("PATCH", "/api/settings", body.dump());
      if (!response.ok) {
        set_status(fmt::format("Could not save replay selections: {}", response.error));
        return;
      }
      download_selected_pack(selected_replays, category, true, selection_revision);
    });
  }

  void draw(bool* open) {
    bool do_refresh = false;
    std::vector<ReplayInfo> replays;
    std::vector<RunnerInfo> runners;
    std::vector<std::string> selected_replays;
    std::string active_category;
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
      selected_replays = m_selected_replay_ids;
      active_category = m_active_category;
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
      ImGui::SetClipboardText("http://127.0.0.1:7878/");
    }
    ImGui::SameLine();
    if (ImGui::Button("Copy player ID")) {
      ImGui::SetClipboardText(persistent_player_id().c_str());
    }
    ImGui::Text("Player ID: %s", persistent_player_id().c_str());
    ImGui::Separator();

    ImGui::Text("Mission: %s", active_category.empty() ? "select one in the Speedrun Menu"
                                                       : active_category.c_str());
    ImGui::TextUnformatted("Race against (click to toggle):");
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
          const auto runner = std::find_if(
              runners.begin(), runners.end(),
              [&replay](const auto& item) { return item.id == replay.src_runner_id; });
          if (runner != runners.end()) {
            runner_name = runner->display_name;
          }
        }
        const auto label =
            fmt::format("{}  ({:.3f}s)##{}", runner_name, replay.time_seconds, replay.id);
        ImGui::PushStyleColor(ImGuiCol_Text, selected ? ImVec4(0.25f, 1.f, 0.35f, 1.f)
                                                     : ImVec4(1.f, 0.25f, 0.25f, 1.f));
        if (ImGui::Selectable(label.c_str(), selected)) {
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
      job();
    }
  }

  void download_selected_pack(const std::vector<std::string>& replay_ids,
                              const std::string& category,
                              bool announce,
                              int selection_revision) {
    if (category.empty()) {
      return;
    }
    std::vector<ReplayInfo> selected;
    {
      std::lock_guard lock(m_state_mutex);
      for (const auto& replay_id : replay_ids) {
        const auto found = std::find_if(
            m_replays.begin(), m_replays.end(),
            [&](const auto& replay) { return replay.id == replay_id; });
        if (found != m_replays.end() && found->category == category &&
            selected.size() < kMaxSelectedReplays) {
          selected.push_back(*found);
        }
      }
    }
    if (announce) {
      set_status(selected.empty() ? "Replay opponents disabled"
                                  : fmt::format("Downloading {} selected replays...", selected.size()));
    }
    try {
      file_util::create_dir_if_needed("ghost");
      for (size_t index = 0; index < selected.size(); ++index) {
        auto response = request(
            "GET", fmt::format("/api/replays/{}/download", selected.at(index).id));
        if (!response.ok) {
          set_status(fmt::format("Could not download {}: {}", selected.at(index).display_name,
                                 response.error));
          return;
        }
        file_util::write_text_file(fmt::format("ghost/selected-replay-{}.json", index),
                                   response.body);
      }
      std::lock_guard lock(m_state_mutex);
      if (selection_revision != m_selection_revision || category != m_active_category) {
        return;
      }
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
      set_status(fmt::format("Could not save selected replays: {}", e.what()));
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

  std::mutex m_state_mutex;
  std::vector<ReplayInfo> m_replays;
  std::vector<RunnerInfo> m_runners;
  std::vector<std::string> m_selected_replay_ids;
  std::vector<std::string> m_ready_replay_ids;
  std::string m_active_category;
  std::string m_ready_category;
  std::string m_status = "Replay server has not been contacted";
  int m_ready_generation = 0;
  int m_selection_revision = 0;
  bool m_connected = false;
  bool m_refresh_started = false;
};

Client& client() {
  static Client instance;
  return instance;
}

}  // namespace

void refresh() {
  client().refresh();
}

void publish(const std::string& replay_path,
             const std::string& category,
             float time_seconds,
             const std::string& src_level_id,
             const std::string& src_category_id,
             const std::string& vehicle_name,
             bool is_personal_best) {
  client().publish(replay_path, category, time_seconds, src_level_id, src_category_id, vehicle_name,
                   is_personal_best);
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

std::string ready_replay_name(int index) {
  return client().ready_replay_name(index);
}

std::string status() {
  return client().status();
}

std::string player_id() {
  return persistent_player_id();
}

void draw_window(bool* open) {
  client().draw(open);
}

}  // namespace replay_client
