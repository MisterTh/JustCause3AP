cmake_minimum_required(VERSION 3.20)
project(JC3APBridge LANGUAGES CXX)

include(FetchContent)

FetchContent_Declare(
    nlohmann_json
    GIT_REPOSITORY https://github.com/nlohmann/json.git
    GIT_TAG v3.11.3
    GIT_SHALLOW TRUE
)
FetchContent_MakeAvailable(nlohmann_json)

add_library(jc3_ap_bridge SHARED
    game_bridge.cpp
    jc3_live_backend.cpp
    ipc_server.cpp
    plugin_entry.cpp
)

target_compile_features(jc3_ap_bridge PRIVATE cxx_std_20)
target_compile_definitions(jc3_ap_bridge PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
target_link_libraries(jc3_ap_bridge PRIVATE nlohmann_json::nlohmann_json ws2_32)

add_executable(jc3_ap_bridge_test_host
    game_bridge.cpp
    jc3_live_backend.cpp
    ipc_server.cpp
    game_bridge_test_host.cpp
)
target_compile_features(jc3_ap_bridge_test_host PRIVATE cxx_std_20)
target_compile_definitions(jc3_ap_bridge_test_host PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
target_link_libraries(jc3_ap_bridge_test_host PRIVATE nlohmann_json::nlohmann_json ws2_32)

if(MSVC)
    target_compile_options(jc3_ap_bridge PRIVATE /W4 /permissive- /EHsc)
    target_compile_options(jc3_ap_bridge_test_host PRIVATE /W4 /permissive- /EHsc)
endif()

set_target_properties(jc3_ap_bridge PROPERTIES
    OUTPUT_NAME "jc3_ap_bridge"
    PREFIX ""
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/../build/$<CONFIG>"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/../build/$<CONFIG>"
)
set_target_properties(jc3_ap_bridge_test_host PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/../build/$<CONFIG>"
)
