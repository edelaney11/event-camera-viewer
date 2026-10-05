project(tcp_event_streamer)

cmake_minimum_required(VERSION 2.8)

if (${CMAKE_SYSTEM_PROCESSOR} MATCHES "arm")
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -mfp16-format=ieee")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -mfp16-format=ieee")
endif (${CMAKE_SYSTEM_PROCESSOR} MATCHES "arm")

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(prophesee_driver REQUIRED)
set(common_libraries ${common_libraries} ${PROPHESEE_DRIVER_LIBRARIES})
include_directories(${PROPHESEE_DRIVER_INCLUDE_DIRS})

find_package(Boost COMPONENTS program_options REQUIRED)
set(common_libraries ${common_libraries} ${Boost_LIBRARIES})

# prophesee_driver.h unconditionally includes <opencv2/core/core.hpp> (it's
# used in a couple of API signatures this tool never calls, e.g. the
# exposure-frame callback). We only need the 'core' component to satisfy
# that include — deliberately NOT pulling in highgui or anything else that
# would drag in GTK/X11, since this streamer is headless by design.
find_package(OpenCV COMPONENTS core REQUIRED)
set(common_libraries ${common_libraries} ${OpenCV_LIBS})
include_directories(${OpenCV_INCLUDE_DIRS})

find_package(Threads REQUIRED)
set(common_libraries ${common_libraries} ${CMAKE_THREAD_LIBS_INIT})

add_executable(tcp_event_streamer tcp_event_streamer.cpp)
target_link_libraries(tcp_event_streamer ${common_libraries})
