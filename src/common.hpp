#pragma once
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <limits>
#include <map>
#include <stdexcept>
#include <string>
#include <time.h>

namespace ep {
inline int64_t clock_ns(clockid_t clock = CLOCK_MONOTONIC) {
  timespec ts{};
  if (clock_gettime(clock, &ts) != 0) throw std::runtime_error("clock_gettime failed");
  return int64_t(ts.tv_sec) * 1000000000LL + ts.tv_nsec;
}
inline void sleep_until(int64_t target) {
  timespec ts{time_t(target / 1000000000LL), long(target % 1000000000LL)};
  int rc;
  do { rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, nullptr); } while (rc == EINTR);
  if (rc) throw std::runtime_error("clock_nanosleep failed: " + std::to_string(rc));
}
inline void busy_for(int64_t ns) {
  const auto end = clock_ns() + ns;
  while (clock_ns() < end) { }
}
struct Args {
  std::map<std::string, std::string> values;
  Args(int argc, char **argv) {
    for (int i = 1; i < argc; i += 2) {
      if (i + 1 >= argc || std::string(argv[i]).rfind("--", 0) != 0)
        throw std::runtime_error("arguments must be --name value pairs");
      if (!values.emplace(argv[i], argv[i + 1]).second)
        throw std::runtime_error("duplicate argument: " + std::string(argv[i]));
    }
  }
  std::string text(const std::string &key) const {
    auto it = values.find(key);
    if (it == values.end()) throw std::runtime_error("missing argument: " + key);
    return it->second;
  }
  int64_t number(const std::string &key, int64_t low, int64_t high) const {
    auto s = text(key); size_t end = 0;
    auto n = std::stoll(s, &end);
    if (end != s.size() || n < low || n > high) throw std::runtime_error("invalid argument: " + key);
    return n;
  }
};
inline std::ofstream output(const std::string &path) {
  std::ofstream f(path, std::ios::out | std::ios::trunc);
  f.exceptions(std::ios::failbit | std::ios::badbit);
  return f;
}
}  // namespace ep
