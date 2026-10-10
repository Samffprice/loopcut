# SPDX-License-Identifier: GPL-2.0-or-later

# CPack runs this after installation and before creating any release archive.
if(NOT DEFINED CPACK_TEMPORARY_DIRECTORY OR NOT IS_DIRECTORY "${CPACK_TEMPORARY_DIRECTORY}")
  message(FATAL_ERROR "Release hygiene: CPack staging directory is unavailable")
endif()

file(GLOB_RECURSE _loopcut_release_entries
  LIST_DIRECTORIES true
  RELATIVE "${CPACK_TEMPORARY_DIRECTORY}"
  "${CPACK_TEMPORARY_DIRECTORY}/*"
)
# A bundle reused from earlier launches (the smoke check, a developer's run) can hold caches that
# install(TARGETS) copies with it: remove them from the staging tree, never from the build.
set(_loopcut_removed 0)
foreach(_entry IN LISTS _loopcut_release_entries)
  if(_entry MATCHES "(^|/)__pycache__$" OR (_entry MATCHES "\\.py[co]$" AND NOT _entry MATCHES "(^|/)__pycache__/"))
    file(REMOVE_RECURSE "${CPACK_TEMPORARY_DIRECTORY}/${_entry}")
    math(EXPR _loopcut_removed "${_loopcut_removed} + 1")
  endif()
endforeach()
message(STATUS "Release hygiene: removed ${_loopcut_removed} Python cache entries from the package")
unset(_loopcut_removed)
unset(_entry)
unset(_loopcut_release_entries)
