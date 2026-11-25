# sr-build injects this through CMAKE_PROJECT_INCLUDE for BE unit tests in
# --dynlib mode, without modifying the source tree.
#
# Upstream ADD_BE_LIB attaches StarRocksPCH to STATIC targets but omits SHARED
# targets. With ENABLE_MULTI_DYNAMIC_LIBS=ON, transitive standard headers may
# therefore disappear; predicate_tree_fwd.h, for example, uses std::vector while
# including only <variant>.
#
# SR_DYNLIB_PCH selects the fix: ON attaches the upstream starrocks_pch.h to shared
# targets; OFF only forces -include vector, without generating additional files.
# WITH_PCH=OFF or a missing PCH header always falls back to -include vector.
#
# CMAKE_PROJECT_INCLUDE runs before subdirectories exist. Defer the recursive
# walk until top-level configuration finishes, then attach PCH to existing targets.
# Deferring directly into a not-yet-created subdirectory is invalid.
# Use add_compile_options so later CMAKE_CXX_FLAGS assignments cannot erase the
# forced include; SHELL: keeps its arguments grouped during CMake deduplication.

option(SR_DYNLIB_PCH "sr: .so 模式下给目标补挂 PCH（OFF 则只 -include vector）" OFF)

# CMAKE_PROJECT_INCLUDE runs after every project(), including nested projects.
# Register the deferred walk and forced include only once, from the top-level
# project, to avoid duplicate execution and repeated options.
get_property(_sr_dynlib_done GLOBAL PROPERTY SR_DYNLIB_COMPAT_DONE)
if(_sr_dynlib_done)
  return()
endif()
set_property(GLOBAL PROPERTY SR_DYNLIB_COMPAT_DONE TRUE)

function(_sr_dynlib_include_vector why)
  message(STATUS "sr dynlib-compat: -include vector（${why}）")
  add_compile_options("$<$<COMPILE_LANGUAGE:CXX>:SHELL:-include vector>")
endfunction()

if(NOT SR_DYNLIB_PCH)
  _sr_dynlib_include_vector("SR_DYNLIB_PCH=OFF")
  return()
endif()

set(_sr_pch "${CMAKE_SOURCE_DIR}/src/pch/starrocks_pch.h")
if(NOT EXISTS "${_sr_pch}")
  _sr_dynlib_include_vector("树里没有 src/pch/starrocks_pch.h")
  return()
endif()

# Attach PCH only to ordinary targets that compile C++. Exclude imported, interface,
# and utility targets; targets with their own PCH; targets inheriting PCH from
# StarRocksPCH; and targets with DISABLE_PRECOMPILE_HEADERS.
# Restrict headers to CXX: mixed-language targets otherwise create unused empty
# C PCH files because starrocks_pch.h is guarded by __cplusplus.
function(_sr_attach_pch_walk d)
  get_property(_tgts DIRECTORY "${d}" PROPERTY BUILDSYSTEM_TARGETS)
  foreach(t IN LISTS _tgts)
    get_target_property(_ty ${t} TYPE)
    if(NOT _ty MATCHES "^(STATIC_LIBRARY|SHARED_LIBRARY|MODULE_LIBRARY|OBJECT_LIBRARY|EXECUTABLE)$")
      continue()
    endif()
    get_target_property(_imp ${t} IMPORTED)
    get_target_property(_dis ${t} DISABLE_PRECOMPILE_HEADERS)
    get_target_property(_own ${t} PRECOMPILE_HEADERS)
    get_target_property(_ll  ${t} LINK_LIBRARIES)
    if(_imp OR _dis OR _own OR ("${_ll}" MATCHES "(^|;)StarRocksPCH(;|$)"))
      continue()
    endif()
    target_precompile_headers(${t} PRIVATE "$<$<COMPILE_LANGUAGE:CXX>:${_sr_pch}>")
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")   # Match upstream StarRocksPCH so ccache can reuse Clang PCH files.
      target_compile_options(${t} PRIVATE "$<$<COMPILE_LANGUAGE:CXX>:SHELL:-Xclang -fno-pch-timestamp>")
    endif()
    math(EXPR _n "${_n} + 1")
  endforeach()
  get_property(_subs DIRECTORY "${d}" PROPERTY SUBDIRECTORIES)
  foreach(s IN LISTS _subs)
    _sr_attach_pch_walk("${s}")
  endforeach()
  set(_n "${_n}" PARENT_SCOPE)
endfunction()

function(_sr_attach_pch_all)
  # Respect upstream WITH_PCH=OFF; in that case only supply the missing header.
  if(DEFINED WITH_PCH AND NOT WITH_PCH)
    _sr_dynlib_include_vector("上游 WITH_PCH=OFF")
    return()
  endif()
  set(_n 0)
  _sr_attach_pch_walk("${CMAKE_SOURCE_DIR}")
  message(STATUS "sr dynlib-compat: 已给 ${_n} 个目标补挂 PCH（${_sr_pch}）")
endfunction()
cmake_language(DEFER CALL _sr_attach_pch_all)
