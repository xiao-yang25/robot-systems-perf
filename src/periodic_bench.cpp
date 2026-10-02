#include "common.hpp"
#include <iostream>
#include <vector>

struct Sample { int64_t seq, scheduled, start, finish, cpu; bool measured; };

int main(int argc, char **argv) {
  try {
    ep::Args args(argc, argv);
    if (args.values.count("--load")) {
      if (args.text("--load") != "cpu") throw std::runtime_error("unsupported load");
      while (true) ep::busy_for(100000000LL);
    }
    auto count = args.number("--count", 1, 1000000);
    auto warmup = args.number("--warmup", 0, 1000000);
    if (count + warmup > 1000000) throw std::runtime_error("too many samples");
    auto period = args.number("--period-ns", 100000, 1000000000);
    auto work = args.number("--work-ns", 0, 1000000000);
    auto path = args.text("--output");
    std::vector<Sample> samples; samples.reserve(size_t(count + warmup));
    const auto base = ep::clock_ns() + 100000000LL;
    for (int64_t i = 0; i < count + warmup; ++i) {
      auto scheduled = base + i * period;
      ep::sleep_until(scheduled);
      auto start = ep::clock_ns();
      auto cpu_start = ep::clock_ns(CLOCK_THREAD_CPUTIME_ID);
      if (work) ep::busy_for(work);
      auto cpu_end = ep::clock_ns(CLOCK_THREAD_CPUTIME_ID);
      auto finish = ep::clock_ns();
      samples.push_back({i, scheduled, start, finish, cpu_end - cpu_start, i >= warmup});
    }
    auto out = ep::output(path);
    out << "seq,scheduled_ns,start_ns,finish_ns,cpu_ns,measured\n";
    for (auto &s : samples)
      out << s.seq << ',' << s.scheduled << ',' << s.start << ',' << s.finish << ',' << s.cpu << ',' << s.measured << '\n';
    out.close();
    return 0;
  } catch (const std::exception &e) { std::cerr << e.what() << '\n'; return 1; }
}
