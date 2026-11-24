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

set(JC3AP_BRIDGE_SOURCES
    game_bridge.cpp
    jc3_live_backend.cpp
    ipc_server.cpp
)

add_library(jc3_ap_plugin SHARED
    ${JC3AP_BRIDGE_SOURCES}
    plugin_entry.cpp
)

add_library(dinput8 SHARED
    dinput8_proxy.cpp
    dinput8.def
)
add_dependencies(dinput8 jc3_ap_plugin)

foreach(target jc3_ap_plugin dinput8)
    target_compile_features(${target} PRIVATE cxx_std_20)
    target_compile_definitions(${target} PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
endforeach()

target_compile_definitions(jc3_ap_plugin PRIVATE JC3AP_BUILD_DLL)
target_link_libraries(
    jc3_ap_plugin PRIVATE nlohmann_json::nlohmann_json ws2_32)

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
    target_compile_options(jc3_ap_plugin PRIVATE /W4 /permissive- /EHsc)
    target_compile_options(dinput8 PRIVATE /W4 /permissive- /EHsc)
    target_compile_options(jc3_ap_bridge_test_host PRIVATE /W4 /permissive- /EHsc)
endif()

foreach(target jc3_ap_plugin dinput8)
    set_target_properties(${target} PROPERTIES
        PREFIX ""
        RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/../build/$<CONFIG>"
        LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/../build/$<CONFIG>"
    )
endforeach()
set_target_properties(jc3_ap_bridge_test_host PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/../build/$<CONFIG>"
)
