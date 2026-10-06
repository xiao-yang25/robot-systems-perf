ARG BASE_IMAGE=ros:humble-ros-base-jammy
FROM ${BASE_IMAGE}
ARG BASE_IMAGE_ID=unknown
ARG SOURCE_REVISION=uncommitted
ENV EP_BASE_IMAGE=${BASE_IMAGE_ID} EP_SOURCE_REVISION=${SOURCE_REVISION}
ENV PYTHONDONTWRITEBYTECODE=1 ROS_LOG_DIR=/tmp/robot-reference-logs HOME=/tmp
WORKDIR /app
COPY CMakeLists.txt ./
COPY src ./src
RUN /bin/bash -c 'source /opt/ros/${ROS_DISTRO}/setup.bash && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j2'
COPY perfkit ./perfkit
COPY scripts ./scripts
COPY tests ./tests
COPY configs ./configs
COPY scenarios ./scenarios
RUN dpkg-query -W > /app/dependencies.txt
CMD ["python3", "-m", "scenarios.robot_reference.run", "--scenario", "all", "--output", "/results/reference"]
