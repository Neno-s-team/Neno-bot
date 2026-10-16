cmake_minimum_required(VERSION 3.20)

project(neno_tdlib_sidecar LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_executable(neno-tdlib-sidecar
    src/main.cpp
)

if (WIN32)
    target_compile_definitions(neno-tdlib-sidecar PRIVATE
        NOMINMAX
        WIN32_LEAN_AND_MEAN
        _CRT_SECURE_NO_WARNINGS
    )
    target_link_libraries(neno-tdlib-sidecar PRIVATE kernel32)
else()
    target_link_libraries(neno-tdlib-sidecar PRIVATE dl)
endif()

if (MSVC)
    target_compile_options(neno-tdlib-sidecar PRIVATE /W4 /EHsc)
else()
    target_compile_options(neno-tdlib-sidecar PRIVATE -Wall -Wextra -Wpedantic)
endif()

set_target_properties(neno-tdlib-sidecar PROPERTIES
    OUTPUT_NAME "neno-tdlib-sidecar"
)
