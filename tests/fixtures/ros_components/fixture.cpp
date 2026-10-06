#include <rclcpp/rclcpp.hpp>
#include <rclcpp_components/register_node_macro.hpp>
#include <std_msgs/msg/string.hpp>

namespace perfkit_test {
class FixtureNode : public rclcpp::Node {
public:
  explicit FixtureNode(const rclcpp::NodeOptions & options) : Node("fixture", options) {
    publisher_ = create_publisher<std_msgs::msg::String>("data", rclcpp::QoS(3).reliable());
    subscription_ = create_subscription<std_msgs::msg::String>(
      "data", rclcpp::QoS(3).reliable(), [](std_msgs::msg::String::ConstSharedPtr) {});
  }
private:
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr publisher_;
  rclcpp::Subscription<std_msgs::msg::String>::SharedPtr subscription_;
};
}  // namespace perfkit_test
RCLCPP_COMPONENTS_REGISTER_NODE(perfkit_test::FixtureNode)
