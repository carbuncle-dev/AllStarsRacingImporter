# Builds only the headless CLI tools from TomKannenberg/SASR-Track-Stuff (CppSLib),
# skipping the OpenGL/ImGui editor so no graphics dependencies are needed.
cmake_minimum_required(VERSION 3.22)
project(CppSLibCli LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(SRC "${CMAKE_CURRENT_SOURCE_DIR}/../../third_party/SASR-Track-Stuff")

find_package(ZLIB REQUIRED)

if (MSVC)
    add_compile_options(/bigobj /utf-8 /EHsc /permissive-)
    add_compile_definitions(NOMINMAX _CRT_SECURE_NO_WARNINGS)
endif()

file(GLOB_RECURSE SLLIB_SOURCES "${SRC}/SlLib/*.cpp")
add_library(SlLib STATIC ${SLLIB_SOURCES})
target_include_directories(SlLib PUBLIC "${SRC}")
target_link_libraries(SlLib PRIVATE ZLIB::ZLIB)

add_library(SeEditorCore STATIC "${SRC}/SeEditor/SifParser.cpp")
target_include_directories(SeEditorCore PUBLIC "${SRC}")
target_link_libraries(SeEditorCore PUBLIC ZLIB::ZLIB)

add_library(SeEditorForest STATIC "${SRC}/SeEditor/Forest/ForestTypes.cpp")
target_link_libraries(SeEditorForest PUBLIC SlLib)

foreach(tool forest_to_obj:ForestToObj sif_unpacker:SifUnpacker forest_tree_dump:ForestTreeDump)
    string(REPLACE ":" ";" parts "${tool}")
    list(GET parts 0 name)
    list(GET parts 1 file)
    add_executable(${name} "${SRC}/SeEditor/${file}.cpp")
    target_include_directories(${name} PRIVATE "${SRC}/SeEditor" "${SRC}")
    target_link_libraries(${name} PRIVATE SeEditorCore SeEditorForest SlLib)
endforeach()
