#pragma once
#include "common.hpp"
#include <array>
#include <sstream>
#include <unistd.h>
#if __has_include(<rmw/rmw.h>)
#include <rmw/rmw.h>
#define EP_HAS_RMW_IDENTIFIER 1
#else
#define EP_HAS_RMW_IDENTIFIER 0
#endif

namespace ep {
inline std::string json_string(const std::string &value) {
  const char hex[] = "0123456789abcdef";
  std::string result = "\"";
  for (unsigned char ch : value) {
    if (ch == '"' || ch == '\\') { result += '\\'; result += char(ch); }
    else if (ch < 0x20) {
      result += "\\u00"; result += hex[ch >> 4]; result += hex[ch & 15];
    } else result += char(ch);
  }
  return result + '"';
}

// SFINAE preserves an explicit unavailable result on older endpoint APIs.
template<class Endpoint>
auto actual_qos_evidence(const Endpoint &endpoint, int)
    -> decltype(endpoint.get_actual_qos().get_rmw_qos_profile(), std::string()) {
  try {
    const auto qos = endpoint.get_actual_qos();
    const auto &profile = qos.get_rmw_qos_profile();
    std::ostringstream out;
    out << "{\"available\":true,\"reason\":null,\"source\":\"get_actual_qos\","
        << "\"reliability\":" << static_cast<int>(profile.reliability)
        << ",\"durability\":" << static_cast<int>(profile.durability)
        << ",\"history\":" << static_cast<int>(profile.history)
        << ",\"depth\":" << profile.depth
        << ",\"representation\":\"RMW QoS policy enum integers and size_t depth; "
        << "SYSTEM_DEFAULT/UNKNOWN retain their RMW values, not resolved transport details\"}";
    return out.str();
  } catch (const std::exception &error) {
    return "{\"available\":false,\"source\":\"get_actual_qos\",\"reason\":" +
        json_string(error.what()) + "}";
  }
}
template<class Endpoint>
std::string actual_qos_evidence(const Endpoint &, long) {
  return "{\"available\":false,\"reason\":\"get_actual_qos API unavailable\"}";
}

// Queried once before measurement; an end snapshot does not query a possibly
// shutdown ROS context. Cached values describe the endpoint at the start phase.
struct RuntimeIdentity {
  int64_t query_ns = 0;
  std::string rmw_identifier;
  std::string actual_qos;
};
template<class Endpoint>
RuntimeIdentity runtime_identity(const Endpoint &endpoint) {
  RuntimeIdentity identity;
  identity.query_ns = clock_ns(CLOCK_MONOTONIC);
  identity.actual_qos = actual_qos_evidence(endpoint, 0);
  identity.rmw_identifier = "{\"available\":false,\"reason\":\"RMW identifier API header unavailable\"}";
#if EP_HAS_RMW_IDENTIFIER
  const char *identifier = rmw_get_implementation_identifier();
  identity.rmw_identifier = identifier && *identifier ?
      "{\"available\":true,\"value\":" + json_string(identifier) + ",\"reason\":null}" :
      "{\"available\":false,\"value\":null,\"reason\":\"RMW identifier is empty\"}";
#endif
  return identity;
}

// Only the benchmark itself reads /proc/self/maps. Called outside timed loops.
// Output failures propagate to main and fail the run; partial files are not success.
inline void runtime_evidence(const std::string &prefix, const std::string &role,
                             const std::string &phase, const RuntimeIdentity &identity,
                             const std::string &stage, bool context_ok) {
  if (prefix.empty()) return;
  const auto observed_ns = clock_ns(CLOCK_MONOTONIC);
  const auto maps_path = prefix + "-" + phase + ".maps";
  std::ifstream maps("/proc/self/maps");
  if (!maps.is_open()) throw std::runtime_error("runtime evidence cannot open /proc/self/maps");
  maps.exceptions(std::ios::badbit);
  auto maps_out = output(maps_path);
  std::array<char, 8192> buffer{};
  while (maps) {
    maps.read(buffer.data(), buffer.size());
    if (maps.gcount()) maps_out.write(buffer.data(), maps.gcount());
  }
  if (!maps.eof()) throw std::runtime_error("runtime evidence cannot read /proc/self/maps");
  maps_out.close();
  auto metadata = output(prefix + "-" + phase + ".json");
  metadata << "{\"schema_version\":1,\"pid\":" << getpid()
           << ",\"role\":" << json_string(role) << ",\"phase\":" << json_string(phase)
           << ",\"collection_stage\":" << json_string(stage)
           << ",\"context_ok\":" << (context_ok ? "true" : "false")
           << ",\"context_shutdown_possible\":" <<
              (role == "subscriber" && phase == "end" ? "true" : "false")
           << ",\"clock\":{\"name\":\"CLOCK_MONOTONIC\",\"clock_id\":"
           << static_cast<int>(CLOCK_MONOTONIC) << ",\"observed_ns\":" << observed_ns
           << ",\"unit\":\"ns\"},\"identity_query_phase\":\"start\","
           << "\"identity_query_monotonic_ns\":" << identity.query_ns
           << ",\"identity_fields_basis\":\"start query; end reuses cached RMW/QoS values\","
           << "\"rmw_identifier\":" << identity.rmw_identifier
           << ",\"actual_qos\":" << identity.actual_qos
           << ",\"maps\":{\"available\":true,\"source\":\"/proc/self/maps\",\"path\":"
           << json_string(maps_path) << ",\"reason\":null}}\n";
  metadata.close();
}
}  // namespace ep
#undef EP_HAS_RMW_IDENTIFIER
