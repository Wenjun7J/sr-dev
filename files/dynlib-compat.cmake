# sr-build 在 --dynlib（BE 单测的 .so 模式）下自动注入，经 -DCMAKE_PROJECT_INCLUDE 传入；不改源码树。
#
# 为什么需要：上游 be/CMakeLists.txt 的 ADD_BE_LIB 只给 STATIC 分支挂了 StarRocksPCH，SHARED 分支漏了。
# 于是 ENABLE_MULTI_DYNAMIC_LIBS=ON 时每个 TU 都不带 -include cmake_pch.hxx，那些"靠 PCH 间接可见"的
# 标准库头全部失效。已知一例 be/src/storage_primitive/predicate_tree/predicate_tree_fwd.h 用 std::vector
# 只 include <variant>：error: 'vector' in namespace 'std' does not name a template type
#
# 两种补法，由 -DSR_DYNLIB_PCH 选（sr-build 传入；默认见 sr-build）：
#   ON  —— 给 .so 模式的目标补上上游漏挂的 PCH（与 STATIC 模式同一份 starrocks_pch.h），顺带拿到 PCH 的编译加速。
#   OFF —— 只强制包含缺失的 <vector>（-include vector），不预编译；最小干预，零额外产物。
# 上游 WITH_PCH=OFF、或树里没有 starrocks_pch.h 时，无论选哪种都退回 -include vector。
#
# 机制：CMAKE_PROJECT_INCLUDE 在 project() 末尾被 include，此时子目录都还没进；用 cmake_language(DEFER)
# 预约一个递归函数在顶层目录处理完（所有 add_subdirectory 跑完）后执行，那时整棵目录树已建好，可以逐个
# target_precompile_headers。不能直接 DEFER 到子目录（那时它还不存在，报 "may not be scheduled in directory"）。
# -include vector 走 add_compile_options：它写目录属性，be/CMakeLists.txt 随后 SET(CMAKE_CXX_FLAGS ...) 整个
# 覆写编译标志也冲不掉（最小工程实测）。SHELL: 保持分组不被 CMake 去重拆散（codex）。

option(SR_DYNLIB_PCH "sr: .so 模式下给目标补挂 PCH（OFF 则只 -include vector）" OFF)

# CMAKE_PROJECT_INCLUDE 在【每次】project() 之后都会被 include，这棵树有两处 project()（顶层 + 一个子工程），
# 不设防就会预约两次 DEFER（实测：先在子工程结束时挂 15 个、再在顶层结束挂 109 个，结果对但重复跑）、
# -include vector 也加两遍。只认第一次——它总是顶层的 project()，DEFER 到顶层结束正是我们要的时机。
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

# 只挂在会真正编 C++ 的普通目标上；跳过：
#   - 非 BE 编译目标（IMPORTED / INTERFACE / UTILITY 等，TYPE 不在白名单里）
#   - 已经有自己 PCH 的（如上游给 ExecJoinCore 单独挂的 join_hash_map 那组），重复挂会让 .gch 内容变
#   - 已经通过链接 StarRocksPCH 拿到 INTERFACE PCH 的（test/ 里的 starrocks_test_objs 等）
#   - 显式 DISABLE_PRECOMPILE_HEADERS 的
# 头只对 C++ 源生效：Gutil/mockjvm/Script 有 .c 源，不加语言限定 CMake 会为 C 也各生成一份 cmake_pch.h(.gch)。
# 不至于编错（starrocks_pch.h 整体在 #ifdef __cplusplus 里，C 侧是空 PCH，上游 STATIC 模式也是这么过的），
# 但那是 3 条纯浪费的边和 3 个空产物，限定 CXX 就没有。
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
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")   # 与上游 StarRocksPCH 同款：让 ccache 能命中 clang 的 PCH
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
  # 上游开关：WITH_PCH=OFF 表示这棵树整体不要 PCH，不能违背它；此时只补缺失头
  if(DEFINED WITH_PCH AND NOT WITH_PCH)
    _sr_dynlib_include_vector("上游 WITH_PCH=OFF")
    return()
  endif()
  set(_n 0)
  _sr_attach_pch_walk("${CMAKE_SOURCE_DIR}")
  message(STATUS "sr dynlib-compat: 已给 ${_n} 个目标补挂 PCH（${_sr_pch}）")
endfunction()
cmake_language(DEFER CALL _sr_attach_pch_all)
