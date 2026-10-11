# UE4SS C++ mods of this project, built against third_party/RE-UE4SS (checked out at the commit of the
# UE4SS installed in the game: a C++ mod must match the UE4SS.dll it is loaded by).
#
#   cmake -S tools/ue4ss/cpp -B out/build/ue4ss_mods -G "Visual Studio 17 2022"
#   cmake --build out/build/ue4ss_mods --config Game__Shipping__Win64 --target CustomGrandPrix -- -m -p:VcpkgEnabled=false
#
# (Rust must be on PATH for UE4SS's patternsleuth; -p:VcpkgEnabled=false keeps a user-wide vcpkg MSBuild
# integration from injecting its own fmt headers.)
cmake_minimum_required(VERSION 3.22)
project(AsrUe4ssMods)

add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../../../third_party/RE-UE4SS RE-UE4SS)
add_subdirectory(CustomGrandPrix)
