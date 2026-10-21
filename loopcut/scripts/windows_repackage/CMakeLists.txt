# Packages a Loopcut for Windows that is already built and signed, without compiling: the release
# workflow's `mode: repackage` (see loopcut/scripts/repackage_windows.py). LOOPCUT_TREE holds an earlier
# release's .zip, unpacked, with this commit's Python swapped in, split into one folder per CPack
# component as the base .msi has them (repackage_windows.py split). Configure only, then run CPack here
# exactly as the full build runs it in build/.
#
# It reads the full build's own packaging.cmake, so the .msi and .zip come out of the same CPack
# settings: the UpgradeCode, the install folder, the shortcuts, the file associations
# (blender-launcher.exe --register-allusers in the WiX template), a new ProductCode each time.
# The components are the full build's ("Blender" for the programs, "Libraries" for the C runtime,
# "Unspecified" for everything else): CPack names every .msi row after its component and path, so
# this keeps them all the same (the WiX template refers to CM_FP_Blender.blender_launcher.exe by name).
#
#   cmake -S loopcut/scripts/windows_repackage -B <dir> -G Ninja -DLOOPCUT_SOURCE_DIR=<checkout>
#     -DLOOPCUT_TREE=<folder of component folders> -DCPACK_OVERRIDE_PACKAGENAME=loopcut-vX.Y.Z
#   cpack -G ZIP / cpack -G WIX (in <dir>, with the same CPACK_PRE_BUILD_SCRIPTS as the full build)
#
# Run from a Visual Studio x64 command prompt: the C compiler check sets MSVC and CMAKE_SIZEOF_VOID_P,
# which packaging.cmake (the windows64 name) and CPack's WiX generator (a 64-bit installer into
# Program Files) read, as in the full build.

cmake_minimum_required(VERSION 3.10)
# The full build's project name: packaging.cmake and CPack's defaults derive names from it.
project(Blender C)

foreach(_var LOOPCUT_SOURCE_DIR LOOPCUT_TREE)
  if(NOT ${_var})
    message(FATAL_ERROR "-D${_var}=... is required")
  endif()
endforeach()
file(TO_CMAKE_PATH "${LOOPCUT_SOURCE_DIR}" LOOPCUT_SOURCE_DIR)
file(TO_CMAKE_PATH "${LOOPCUT_TREE}" LOOPCUT_TREE)
if(NOT MSVC OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
  message(FATAL_ERROR "Configure with 64-bit MSVC (vcvars64), as the full build is")
endif()

# Blender's version, read as get_blender_version() in build_files/cmake/macros.cmake reads it.
file(STRINGS "${LOOPCUT_SOURCE_DIR}/source/blender/blenkernel/BKE_blender_version.h" _contents
  REGEX "^#define[ \t]+BLENDER_.*$")
string(REGEX REPLACE ".*#define[ \t]+BLENDER_VERSION[ \t]+([0-9]+).*" "\\1" _version "${_contents}")
string(REGEX REPLACE ".*#define[ \t]+BLENDER_VERSION_PATCH[ \t]+([0-9]+).*" "\\1" _patch "${_contents}")
if(NOT _version MATCHES "^[0-9]+$" OR NOT _patch MATCHES "^[0-9]+$")
  message(FATAL_ERROR "Version parsing failed for BKE_blender_version.h")
endif()
math(EXPR BLENDER_VERSION_MAJOR "${_version} / 100")
math(EXPR BLENDER_VERSION_MINOR "${_version} % 100")
set(BLENDER_VERSION_PATCH "${_patch}")
set(BLENDER_VERSION "${BLENDER_VERSION_MAJOR}.${BLENDER_VERSION_MINOR}")

# Each component folder's contents, as that component.
file(GLOB _components LIST_DIRECTORIES true RELATIVE "${LOOPCUT_TREE}" "${LOOPCUT_TREE}/*")
if(NOT "Blender" IN_LIST _components OR NOT "Unspecified" IN_LIST _components)
  message(FATAL_ERROR "${LOOPCUT_TREE} must hold a folder per CPack component, found: ${_components}")
endif()
foreach(_component IN LISTS _components)
  install(DIRECTORY "${LOOPCUT_TREE}/${_component}/" DESTINATION "." COMPONENT "${_component}" USE_SOURCE_PERMISSIONS)
endforeach()

# The full build's packaging.cmake, with its source paths (the WiX template, icons, license) pointed
# at the checkout instead of this folder.
file(READ "${LOOPCUT_SOURCE_DIR}/build_files/cmake/packaging.cmake" _packaging)
string(REPLACE "\${CMAKE_SOURCE_DIR}" "${LOOPCUT_SOURCE_DIR}" _packaging "${_packaging}")
file(WRITE "${CMAKE_BINARY_DIR}/packaging.cmake" "${_packaging}")
include("${CMAKE_BINARY_DIR}/packaging.cmake")
