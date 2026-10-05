project(genx320_streamer)

cmake_minimum_required(VERSION 3.10)

if (${CMAKE_SYSTEM_PROCESSOR} MATCHES "arm")
    set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -mfp16-format=ieee")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -mfp16-format=ieee")
endif (${CMAKE_SYSTEM_PROCESSOR} MATCHES "arm")

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Built against OpenEB's own open-source C++ SDK — not the closed-source
# prophesee_driver SDK onboard_streamer/ links against, since a GenX320
# build has no such closed SDK to link against at all. Point CMake at an
# OpenEB install the same way the rest of this repo locates it (see
# sdk_bootstrap.py): either CMAKE_PREFIX_PATH already includes it, or pass
# -DCMAKE_PREFIX_PATH=/path/to/openeb/install on the cmake invocation.
find_package(MetavisionSDK COMPONENTS stream REQUIRED)
find_package(MetavisionHAL REQUIRED)

find_package(Boost COMPONENTS program_options REQUIRED)

find_package(Threads REQUIRED)

add_executable(genx320_streamer genx320_streamer.cpp)
target_link_libraries(genx320_streamer
    MetavisionSDK::stream
    Metavision::HAL
    Metavision::HAL_discovery
    ${Boost_LIBRARIES}
    ${CMAKE_THREAD_LIBS_INIT}
)
