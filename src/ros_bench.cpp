#include "common.hpp"
#include <rclcpp/rclcpp.hpp>
#include "runtime_evidence.hpp"
#include <std_msgs/msg/byte_multi_array.hpp>
#include <iostream>
#include <memory>
#include <vector>

// Header: seq, planned release, generated time, pre-publish time; little-endian.
// Payload bytes exclude the 32-byte header and ROS serialization overhead.
constexpr size_t header_bytes = 32;
void put(std::vector<uint8_t> &v, size_t offset, uint64_t value) {
  for (size_t i = 0; i < 8; ++i) v[offset + i] = uint8_t(value >> (8 * i));
}
uint64_t get(const std::vector<uint8_t> &v, size_t offset) {
  uint64_t value = 0;
  for (size_t i = 0; i < 8; ++i) value |= uint64_t(v[offset + i]) << (8 * i);
  if (value > uint64_t(std::numeric_limits<int64_t>::max())) throw std::runtime_error("header overflow");
  return value;
}
struct Sent { int64_t seq, scheduled, generated, publish, returned; bool measured; };
struct Received { int64_t seq, receive, finish, cpu; bool valid; };

int main(int argc, char **argv) {
  try {
    ep::Args args(argc, argv);
    const auto role = args.text("--role");
    if (role != "publisher" && role != "subscriber") throw std::runtime_error("invalid role");
    const auto count = args.number("--count", 1, 1000000);
    const auto warmup = args.number("--warmup", 0, 1000000);
    if (count + warmup > 1000000) throw std::runtime_error("too many samples");
    const auto payload = args.number("--payload-bytes", 0, 16777216);
    const auto depth = args.number("--depth", 1, 100000);
    const auto reliability = args.text("--reliability");
    if (reliability != "reliable" && reliability != "best_effort") throw std::runtime_error("invalid reliability");
    const auto out_path = args.text("--output");
    const auto topic = args.text("--topic");
    const auto evidence_arg = args.values.find("--runtime-evidence");
    const auto evidence_prefix = evidence_arg == args.values.end() ? std::string() : evidence_arg->second;
    if (evidence_arg != args.values.end() && evidence_prefix.empty())
      throw std::runtime_error("empty runtime evidence prefix");
    // Program arguments are not ROS arguments; initialize without CLI parsing.
    rclcpp::init(0, nullptr);
    auto node = std::make_shared<rclcpp::Node>("embodied_perf_" + role);
    auto qos = rclcpp::QoS(rclcpp::KeepLast(size_t(depth)));
    if (reliability == "reliable") qos.reliable(); else qos.best_effort();
    qos.durability_volatile();
    if (role == "publisher") {
      auto period = args.number("--period-ns", 100000, 1000000000);
      auto pub = node->create_publisher<std_msgs::msg::ByteMultiArray>(topic, qos);
      const auto discovery_end = ep::clock_ns() + 15000000000LL;
      while (rclcpp::ok() && pub->get_subscription_count() == 0 && ep::clock_ns() < discovery_end) {
        rclcpp::spin_some(node);
        ep::sleep_until(ep::clock_ns() + 20000000LL);
      }
      if (!rclcpp::ok() || pub->get_subscription_count() == 0) throw std::runtime_error("subscriber discovery timeout");
      std_msgs::msg::ByteMultiArray message;
      message.data.resize(header_bytes + size_t(payload), uint8_t(0xA5));
      std::vector<Sent> sent; sent.reserve(size_t(count + warmup));
      const auto runtime = evidence_prefix.empty() ? ep::RuntimeIdentity{} : ep::runtime_identity(*pub);
      ep::runtime_evidence(evidence_prefix, role, "start", runtime,
                           "discovery_complete_message_allocated_before_schedule", rclcpp::ok());
      const auto base = ep::clock_ns() + 100000000LL;
      for (int64_t i = 0; i < count + warmup; ++i) {
        if (!rclcpp::ok()) throw std::runtime_error("publisher interrupted before completion");
        auto scheduled = base + i * period;
        ep::sleep_until(scheduled);
        auto generated = ep::clock_ns();
        put(message.data, 0, uint64_t(i)); put(message.data, 8, uint64_t(scheduled));
        put(message.data, 16, uint64_t(generated));
        auto publish = ep::clock_ns(); put(message.data, 24, uint64_t(publish));
        pub->publish(message);
        auto returned = ep::clock_ns();
        sent.push_back({i, scheduled, generated, publish, returned, i >= warmup});
      }
      auto out = ep::output(out_path);
      out << "seq,scheduled_ns,generated_ns,publish_ns,publish_return_ns,measured\n";
      for (auto &s : sent)
        out << s.seq << ',' << s.scheduled << ',' << s.generated << ',' << s.publish << ',' << s.returned << ',' << s.measured << '\n';
      out.close();
      ep::runtime_evidence(evidence_prefix, role, "end", runtime,
                           "publish_loop_completed_csv_written", rclcpp::ok());
    } else {
      auto delay = args.number("--callback-delay-ns", 0, 1000000000);
      const size_t capacity = size_t(count + warmup) * 2 + 16;
      std::vector<Received> received; received.reserve(capacity);
      size_t overflow = 0, malformed = 0;
      auto sub = node->create_subscription<std_msgs::msg::ByteMultiArray>(topic, qos,
        [&](const std_msgs::msg::ByteMultiArray::SharedPtr msg) {
          auto receive = ep::clock_ns();
          auto cpu_start = ep::clock_ns(CLOCK_THREAD_CPUTIME_ID);
          if (msg->data.size() < header_bytes) { ++malformed; return; }
          auto seq = int64_t(get(msg->data, 0));
          bool valid = msg->data.size() == header_bytes + size_t(payload);
          // Full payload validation is deliberately part of callback wall time.
          for (size_t i = header_bytes; valid && i < msg->data.size(); ++i)
            if (msg->data[i] != uint8_t(0xA5)) valid = false;
          if (delay) ep::sleep_until(ep::clock_ns() + delay);
          auto cpu_end = ep::clock_ns(CLOCK_THREAD_CPUTIME_ID);
          auto finish = ep::clock_ns();
          if (received.size() < capacity) received.push_back({seq, receive, finish, cpu_end - cpu_start, valid});
          else ++overflow;
        });
      const auto runtime = evidence_prefix.empty() ? ep::RuntimeIdentity{} : ep::runtime_identity(*sub);
      ep::runtime_evidence(evidence_prefix, role, "start", runtime,
                           "subscription_created_before_ready", rclcpp::ok());
      auto ready = ep::output(args.text("--ready-file")); ready << "ready\n"; ready.close();
      rclcpp::spin(node);
      auto out = ep::output(out_path);
      out << "seq,receive_ns,finish_ns,cpu_ns,payload_valid\n";
      for (auto &r : received)
        out << r.seq << ',' << r.receive << ',' << r.finish << ',' << r.cpu << ',' << r.valid << '\n';
      out.close();
      ep::runtime_evidence(evidence_prefix, role, "end", runtime,
                           "spin_returned_csv_written", rclcpp::ok());
      if (overflow || malformed) throw std::runtime_error("receiver recording overflow or malformed messages: " + std::to_string(overflow) + "," + std::to_string(malformed));
    }
    rclcpp::shutdown();
    return 0;
  } catch (const std::exception &e) {
    if (rclcpp::ok()) rclcpp::shutdown();
    std::cerr << e.what() << '\n'; return 1;
  }
}
