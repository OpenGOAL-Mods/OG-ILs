#pragma once

#include <functional>
#include <optional>
#include <string>

namespace replay_client {

using ReplayLevelResolver = std::function<std::optional<std::string>(const std::string& category)>;

void refresh();
void publish(const std::string& replay_path, ReplayLevelResolver level_resolver);
int prepare_selected(const std::string& category);
int selected_count();
int mission_replay_count();
std::string mission_replay_label(int index);
bool mission_replay_selected(int index);
bool toggle_mission_replay(int index);
int mode_count();
std::string mode_label(int index);
bool mode_selected(int index);
bool custom_mode_selected();
bool set_mode(int index);
std::string ready_replay_name(int index);
std::string status();
std::string player_id();
std::string player_name();
bool ping_unknown_player();
void refresh_point_leaderboard();
bool select_point_leaderboard(int mode_index, int group_index);
int point_leaderboard_state();
int point_leaderboard_count();
std::string point_leaderboard_label(int index);
int point_leaderboard_value(int index, int field);
std::string point_leaderboard_text(int index, int field);
int point_leaderboard_mode_count();
std::string point_leaderboard_mode_label(int index);
int point_leaderboard_mode_index();
bool set_point_leaderboard_mode(int index);
int point_leaderboard_group_count();
std::string point_leaderboard_group_label(int index);
int point_leaderboard_group_index();
bool set_point_leaderboard_group(int index);
std::string point_leaderboard_status();
void draw_window(bool* open);

}  // namespace replay_client
