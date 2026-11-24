// Loads a scs-telemetry.dll the way the game does and plays a short session
// into it: configuration, frames with every registered channel, gameplay
// events, shutdown (twice) and unload. It reads the plugin's shared memory from the
// outside, as the app does, and reports what it saw.
//
//   host.exe <dll> [--ats] [--dump <file>] [--check]
//
// --dump writes the shared memory as it stood after the frames (before the
// gameplay events), to compare two builds byte by byte. --check makes the
// exit code 1 unless every fact below reads as the SCS Haptics build should.
#include <windows.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "amtrucks/scssdk_ats.h"
#include "amtrucks/scssdk_telemetry_ats.h"
#include "eurotrucks2/scssdk_eut2.h"
#include "eurotrucks2/scssdk_telemetry_eut2.h"
#include "scssdk_telemetry.h"

namespace {

struct Channel {
  std::string name;
  scs_u32_t index;
  scs_value_type_t type;
  scs_u32_t flags;
  scs_telemetry_channel_callback_t callback;
  scs_context_t context;
};

std::vector<Channel> channels;
scs_telemetry_event_callback_t event_callbacks[8];
scs_context_t event_contexts[8];
std::vector<std::string> game_log;
int warnings = 0;

SCSAPI_VOID host_log(const scs_log_type_t type, const scs_string_t message) {
  if (type != SCS_LOG_TYPE_message) {
    ++warnings;
  }
  game_log.push_back((type == SCS_LOG_TYPE_message ? "message: " : type == SCS_LOG_TYPE_warning ? "WARNING: " : "ERROR: ") +
                     std::string(message));
}

SCSAPI_RESULT register_for_event(const scs_event_t event, const scs_telemetry_event_callback_t callback,
                                 const scs_context_t context) {
  if (event >= 8) {
    return SCS_RESULT_invalid_parameter;
  }
  event_callbacks[event] = callback;
  event_contexts[event] = context;
  return SCS_RESULT_ok;
}

SCSAPI_RESULT unregister_from_event(const scs_event_t event) {
  if (event < 8) {
    event_callbacks[event] = nullptr;
  }
  return SCS_RESULT_ok;
}

SCSAPI_RESULT register_for_channel(const scs_string_t name, const scs_u32_t index, const scs_value_type_t type,
                                   const scs_u32_t flags, const scs_telemetry_channel_callback_t callback,
                                   const scs_context_t context) {
  channels.push_back({name, index, type, flags, callback, context});
  return SCS_RESULT_ok;
}

SCSAPI_RESULT unregister_from_channel(const scs_string_t, const scs_u32_t, const scs_value_type_t) {
  return SCS_RESULT_ok;
}

void fire(const scs_event_t event, const void* info) {
  if (event_callbacks[event]) {
    event_callbacks[event](event, info, event_contexts[event]);
  }
}

unsigned hash_of(const std::string& text, unsigned seed) {
  unsigned h = 2166136261u ^ seed;
  for (const char c : text) {
    h = (h ^ static_cast<unsigned char>(c)) * 16777619u;
  }
  return h;
}

// A value for the channel in this frame: made from its name, so two builds
// given the same registrations get the same memory.
bool value_for(const Channel& channel, const unsigned frame, scs_value_t& value) {
  // only two trailers exist; the rest have no value, as in the game
  if (channel.name.rfind("trailer.", 0) == 0 && channel.name.size() > 8 && channel.name[8] >= '2' &&
      channel.name[8] <= '9') {
    return false;
  }
  const unsigned h = hash_of(channel.name, channel.index * 31u + frame * 7u);
  const float f = static_cast<float>(h % 20000u) / 100.0f - 50.0f;
  memset(&value, 0, sizeof value);
  value.type = channel.type;
  switch (channel.type) {
    case SCS_VALUE_TYPE_bool:
      value.value_bool.value = static_cast<scs_u8_t>(h & 1u);
      return true;
    case SCS_VALUE_TYPE_s32:
      value.value_s32.value = static_cast<scs_s32_t>(h % 13u) - 2;
      return true;
    case SCS_VALUE_TYPE_u32:
      value.value_u32.value = h % 17u;
      return true;
    case SCS_VALUE_TYPE_float:
      value.value_float.value = f;
      return true;
    case SCS_VALUE_TYPE_fvector:
      value.value_fvector = {f, f * 0.5f, f * 0.25f};
      return true;
    case SCS_VALUE_TYPE_fplacement:
      value.value_fplacement.position = {f, f * 0.5f, f * 0.25f};
      value.value_fplacement.orientation = {f * 0.01f, f * 0.02f, f * 0.03f};
      return true;
    case SCS_VALUE_TYPE_dplacement:
      value.value_dplacement.position = {f * 100.0, f * 50.0, f * 25.0};
      value.value_dplacement.orientation = {f * 0.01f, f * 0.02f, f * 0.03f};
      return true;
    default:
      return false;
  }
}

scs_named_value_t named(const char* name, const scs_u32_t index, const scs_value_t& value) {
  scs_named_value_t out;
  memset(&out, 0, sizeof out);
  out.name = name;
  out.index = index;
  out.value = value;
  return out;
}

scs_value_t v_string(const char* text) {
  scs_value_t v;
  memset(&v, 0, sizeof v);
  v.type = SCS_VALUE_TYPE_string;
  v.value_string.value = text;
  return v;
}

scs_value_t v_u32(const scs_u32_t x) {
  scs_value_t v;
  memset(&v, 0, sizeof v);
  v.type = SCS_VALUE_TYPE_u32;
  v.value_u32.value = x;
  return v;
}

scs_value_t v_s64(const scs_s64_t x) {
  scs_value_t v;
  memset(&v, 0, sizeof v);
  v.type = SCS_VALUE_TYPE_s64;
  v.value_s64.value = x;
  return v;
}

scs_value_t v_float(const float x) {
  scs_value_t v;
  memset(&v, 0, sizeof v);
  v.type = SCS_VALUE_TYPE_float;
  v.value_float.value = x;
  return v;
}

scs_value_t v_fvector(const float x, const float y, const float z) {
  scs_value_t v;
  memset(&v, 0, sizeof v);
  v.type = SCS_VALUE_TYPE_fvector;
  v.value_fvector = {x, y, z};
  return v;
}

void configure(const char* id, std::vector<scs_named_value_t> attributes) {
  scs_named_value_t end;
  memset(&end, 0, sizeof end);
  attributes.push_back(end);
  const scs_telemetry_configuration_t info = {id, attributes.data()};
  fire(SCS_TELEMETRY_EVENT_configuration, &info);
}

void gameplay(const char* id, std::vector<scs_named_value_t> attributes) {
  scs_named_value_t end;
  memset(&end, 0, sizeof end);
  attributes.push_back(end);
  const scs_telemetry_gameplay_event_t info = {id, attributes.data()};
  fire(SCS_TELEMETRY_EVENT_gameplay, &info);
}

// offsets of the shared memory (plugin revision 12)
constexpr size_t SDK_ACTIVE = 0, RENDER_TIME = 24, REVISION = 40, GAME = 52, TELEMETRY_MINOR = 60;
constexpr size_t PLUGIN_BUILD_AT = 452, TRAILER_DEFLECTION = 168, TRAILER_ID = 920, TRAILER_WHEEL_COUNT = 148;
constexpr size_t MANDATORY_BREAK = 644, SPEED = 948, PENALTY = 4200, FLAGS = 4300, SUBSTANCES = 4400;
constexpr size_t TRAILERS = 6000, TRAILER_SIZE = 1560, TRAILER_LIFT = 152 + 16 + 4 * 64, MAP_SIZE = 6000 + 10 * 1560;
constexpr size_t FLAG_CANCELLED = FLAGS + 2, FLAG_TOLLGATE = FLAGS + 5;

template <typename T>
T at(const unsigned char* memory, const size_t offset) {
  T out;
  memcpy(&out, memory + offset, sizeof out);
  return out;
}

// The call DllMain makes as the DLL unloads, after the game's own shutdown call.
// Made directly: Windows swallows a fault inside DllMain on FreeLibrary, but not
// always on the way out of a process.
bool shutdown_again(SCSAPI_VOID_FPTR(shutdown)()) {
  __try {
    shutdown();
    return true;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
}

}  // namespace

int main(const int argc, char** argv) {
  if (argc < 2) {
    printf("usage: host <scs-telemetry.dll> [--ats] [--dump <file>] [--check]\n");
    return 2;
  }
  bool ats = false, check = false;
  const char* dump = nullptr;
  for (int i = 2; i < argc; ++i) {
    if (strcmp(argv[i], "--ats") == 0) {
      ats = true;
    } else if (strcmp(argv[i], "--check") == 0) {
      check = true;
    } else if (strcmp(argv[i], "--dump") == 0 && i + 1 < argc) {
      dump = argv[++i];
    }
  }

  const HMODULE dll = LoadLibraryA(argv[1]);
  if (!dll) {
    printf("cannot load %s (error %lu)\n", argv[1], GetLastError());
    return 2;
  }
  typedef SCSAPI_RESULT_FPTR(init_t)(scs_u32_t, const scs_telemetry_init_params_t*);
  typedef SCSAPI_VOID_FPTR(shutdown_t)();
  const auto init = reinterpret_cast<init_t>(GetProcAddress(dll, "scs_telemetry_init"));
  const auto shutdown = reinterpret_cast<shutdown_t>(GetProcAddress(dll, "scs_telemetry_shutdown"));
  if (!init || !shutdown) {
    printf("the DLL does not export scs_telemetry_init / scs_telemetry_shutdown\n");
    return 2;
  }

  // the telemetry versions the 1.62 games report (their game.log.txt)
  scs_telemetry_init_params_v101_t params;
  memset(&params, 0, sizeof params);
  params.common.game_name = ats ? "American Truck Simulator" : "Euro Truck Simulator 2";
  params.common.game_id = ats ? SCS_GAME_ID_ATS : SCS_GAME_ID_EUT2;
  params.common.game_version = ats ? SCS_MAKE_VERSION(1, 8) : SCS_MAKE_VERSION(1, 22);
  params.common.log = host_log;
  params.register_for_event = register_for_event;
  params.unregister_from_event = unregister_from_event;
  params.register_for_channel = register_for_channel;
  params.unregister_from_channel = unregister_from_channel;

  const scs_result_t result = init(SCS_TELEMETRY_VERSION_1_01, &params);
  printf("init: %s, %zu channels registered\n", result == SCS_RESULT_ok ? "ok" : "FAILED", channels.size());
  if (result != SCS_RESULT_ok) {
    return 2;
  }

  const HANDLE mapping = OpenFileMappingW(FILE_MAP_READ, FALSE, L"Local\\SCSTelemetry");
  const auto memory = mapping ? static_cast<const unsigned char*>(MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, 0)) : nullptr;
  if (!memory) {
    printf("the plugin made no Local\\SCSTelemetry mapping\n");
    return 2;
  }
  printf("revision %u, game %u, telemetry version 1.%u\n", at<unsigned>(memory, REVISION), at<unsigned>(memory, GAME),
         at<unsigned>(memory, TELEMETRY_MINOR));

  // -- configuration, as on loading a save ---------------------------------
  configure(SCS_TELEMETRY_CONFIG_substances,
            {named(SCS_TELEMETRY_CONFIG_ATTRIBUTE_id, 0, v_string("static")),
             named(SCS_TELEMETRY_CONFIG_ATTRIBUTE_id, 1, v_string("road")),
             named(SCS_TELEMETRY_CONFIG_ATTRIBUTE_id, 2, v_string("dirt"))});
  configure(SCS_TELEMETRY_CONFIG_truck,
            {named(SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_count, SCS_U32_NIL, v_u32(6)),
             named(SCS_TELEMETRY_CONFIG_ATTRIBUTE_rpm_limit, SCS_U32_NIL, v_float(2300.0f)),
             named(SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_position, 0, v_fvector(-1.0f, 0.5f, -1.5f)),
             named(SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_position, 1, v_fvector(1.0f, 0.5f, -1.5f))});
  configure("trailer.0", {named(SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_count, SCS_U32_NIL, v_u32(4)),
                          named(SCS_TELEMETRY_CONFIG_ATTRIBUTE_id, SCS_U32_NIL, v_string("trailer.first"))});
  configure("trailer.1", {named(SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_count, SCS_U32_NIL, v_u32(2)),
                          named(SCS_TELEMETRY_CONFIG_ATTRIBUTE_id, SCS_U32_NIL, v_string("trailer.second"))});
  configure(SCS_TELEMETRY_CONFIG_job, {named(SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo_id, SCS_U32_NIL, v_string("cargo.test"))});
  const int warnings_before_new_configs = warnings;
  // SDK 1.15's configurations: every 1.62 game sends both, empty without such a job
  configure(SCS_TELEMETRY_CONFIG_car_job, {named(SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo_id, SCS_U32_NIL, v_string("car.cargo"))});
  configure(SCS_TELEMETRY_CONFIG_bus_job, {});
  const int new_config_warnings = warnings - warnings_before_new_configs;
  fire(SCS_TELEMETRY_EVENT_started, nullptr);

  // -- frames ---------------------------------------------------------------
  const unsigned frames = 5;
  int early_times = 0;  // frames whose time was readable before their values
  scs_telemetry_frame_start_t frame;
  memset(&frame, 0, sizeof frame);
  for (unsigned n = 0; n < frames; ++n) {
    frame.render_time = 5000000 + n * 16667ull;
    frame.simulation_time = frame.render_time;
    frame.paused_simulation_time = frame.render_time;
    const auto time_before = at<unsigned long long>(memory, RENDER_TIME);
    fire(SCS_TELEMETRY_EVENT_frame_start, &frame);
    if (n > 0 && at<unsigned long long>(memory, RENDER_TIME) != time_before) {
      ++early_times;
    }
    for (const Channel& channel : channels) {
      scs_value_t value;
      if (value_for(channel, n, value)) {
        channel.callback(channel.name.c_str(), channel.index, &value, channel.context);
      } else if (channel.flags & SCS_TELEMETRY_CHANNEL_FLAG_no_value) {
        channel.callback(channel.name.c_str(), channel.index, nullptr, channel.context);
      }
    }
    fire(SCS_TELEMETRY_EVENT_frame_end, nullptr);
  }
  const auto render_time = at<unsigned long long>(memory, RENDER_TIME);
  const bool time_ok = render_time == (frames - 1) * 16667ull;
  printf("render time after %u frames: %llu us (%s)\n", frames, render_time, time_ok ? "as sent" : "WRONG");
  printf("frames whose time showed before their values: %d of %u\n", early_times, frames - 1);

  // which trailer slot holds trailer 1's wheel lift (frame 4's value of wheel 0)
  scs_value_t lift;
  bool lift_own = false, lift_first = false;
  for (const Channel& channel : channels) {
    if (channel.name == "trailer.1.wheel.lift" && channel.index == 0 && value_for(channel, frames - 1, lift)) {
      lift_own = at<float>(memory, TRAILERS + TRAILER_SIZE + TRAILER_LIFT) == lift.value_float.value;
      lift_first = at<float>(memory, TRAILERS + TRAILER_LIFT) == lift.value_float.value;
    }
  }
  printf("trailer 1's wheel lift is in: %s\n", lift_own ? "trailer 1" : lift_first ? "trailer 0" : "neither");
  printf("mandatory break (offset 644): %d\n", at<int>(memory, MANDATORY_BREAK));
  const unsigned build = at<unsigned>(memory, PLUGIN_BUILD_AT);
  printf("plugin build (offset 452): %u\n", build);
  printf("speed %.2f, substance 1 '%s'\n", at<float>(memory, SPEED), reinterpret_cast<const char*>(memory + SUBSTANCES + 64));

  if (dump) {
    FILE* file = nullptr;
    if (fopen_s(&file, dump, "wb") == 0 && file) {
      fwrite(memory, 1, MAP_SIZE, file);
      fclose(file);
    }
  }

  // -- gameplay events ------------------------------------------------------
  const bool toll_0 = at<bool>(memory, FLAG_TOLLGATE);
  gameplay(SCS_TELEMETRY_GAMEPLAY_EVENT_player_tollgate_paid,
           {named(SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_pay_amount, SCS_U32_NIL, v_s64(12))});
  const bool toll_1 = at<bool>(memory, FLAG_TOLLGATE);
  gameplay(SCS_TELEMETRY_GAMEPLAY_EVENT_player_tollgate_paid,
           {named(SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_pay_amount, SCS_U32_NIL, v_s64(12))});
  const bool toll_2 = at<bool>(memory, FLAG_TOLLGATE);
  const bool toggles = !toll_0 && toll_1 && !toll_2;
  printf("tollgate flag over two tolls: %d -> %d -> %d (%s)\n", toll_0, toll_1, toll_2,
         toggles ? "flips with each event" : "does not flip");

  const int warnings_before_car = warnings;
  const auto penalty_before = at<long long>(memory, PENALTY);
  const bool cancelled_before = at<bool>(memory, FLAG_CANCELLED);
  gameplay(SCS_TELEMETRY_GAMEPLAY_EVENT_car_job_cancelled,
           {named(SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_cancel_penalty, SCS_U32_NIL, v_s64(777))});
  gameplay(SCS_TELEMETRY_GAMEPLAY_EVENT_car_job_delivered,
           {named(SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_revenue, SCS_U32_NIL, v_s64(5000)),
            named(SCS_TELEMETRY_GAMEPLAY_EVENT_ATTRIBUTE_vehicle_damage, SCS_U32_NIL, v_float(0.1f))});
  const bool car_job_kept_out =
      at<long long>(memory, PENALTY) == penalty_before && at<bool>(memory, FLAG_CANCELLED) == cancelled_before;
  const int car_job_warnings = warnings - warnings_before_car;
  printf("a car job's events: %s, %d log warnings\n",
         car_job_kept_out ? "left the truck job's fields alone" : "WROTE INTO the truck job's fields", car_job_warnings);
  printf("car_job / bus_job configurations: %d log warnings\n", new_config_warnings);

  // -- a trailer's configuration changing while it is there, then a trailer going away
  const float deflection_0 = at<float>(memory, TRAILERS + TRAILER_DEFLECTION);
  const float deflection_1 = at<float>(memory, TRAILERS + TRAILER_SIZE + TRAILER_DEFLECTION);
  configure("trailer.0", {named(SCS_TELEMETRY_CONFIG_ATTRIBUTE_wheel_count, SCS_U32_NIL, v_u32(6)),
                          named(SCS_TELEMETRY_CONFIG_ATTRIBUTE_id, SCS_U32_NIL, v_string("trailer.loaded"))});
  const bool config_read = at<unsigned>(memory, TRAILERS + TRAILER_WHEEL_COUNT) == 6 &&
                           strcmp(reinterpret_cast<const char*>(memory + TRAILERS + TRAILER_ID), "trailer.loaded") == 0;
  const bool trailer_kept = deflection_0 != 0.0f && at<float>(memory, TRAILERS + TRAILER_DEFLECTION) == deflection_0;
  configure("trailer.1", {});
  const bool trailer_cleared = deflection_1 != 0.0f &&
                               at<float>(memory, TRAILERS + TRAILER_SIZE + TRAILER_DEFLECTION) == 0.0f &&
                               at<unsigned>(memory, TRAILERS + TRAILER_SIZE + TRAILER_WHEEL_COUNT) == 0;
  printf("trailer 0 given a new configuration: %s, its wheel values %s\n", config_read ? "read" : "NOT READ",
         trailer_kept ? "kept" : "WIPED until the game sends them again");
  printf("trailer 1 gone (empty configuration): %s\n", trailer_cleared ? "cleared" : "NOT CLEARED");

  // -- shutdown and unload, as on leaving the game --------------------------
  shutdown();
  const bool cleared = !at<bool>(memory, SDK_ACTIVE) && at<unsigned>(memory, REVISION) == 0;
  printf("after shutdown: %s\n", cleared ? "inactive, revision 0" : "STILL ACTIVE");
  const bool clean = shutdown_again(shutdown);
  printf("second shutdown, as on unload: %s\n", clean ? "does nothing" : "ACCESS VIOLATION (writes to unmapped memory)");
  FreeLibrary(dll);

  printf("game log (%d warnings or errors):\n", warnings);
  for (const std::string& line : game_log) {
    printf("  %s\n", line.c_str());
  }

  const bool good = time_ok && early_times == 0 && lift_own && toggles && car_job_kept_out && car_job_warnings == 0 &&
                    new_config_warnings == 0 && cleared && clean && build > 0 && config_read && trailer_kept &&
                    trailer_cleared;
  if (check) {
    printf(good ? "CHECK PASSED\n" : "CHECK FAILED\n");
    return good ? 0 : 1;
  }
  return 0;
}
