set(TARGET CustomGrandPrix)
add_library(${TARGET} SHARED src/dllmain.cpp)
target_link_libraries(${TARGET} PUBLIC UE4SS)
target_link_libraries(${TARGET} PRIVATE Zydis PolyHook_2 psapi)
