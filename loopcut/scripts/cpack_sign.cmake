# CPack pre-build script: signs the programs and libraries CPack has staged for a package, before
# it archives them, so the .zip and the files the .msi installs are signed. The Release workflow
# passes it as `cpack -D CPACK_PRE_BUILD_SCRIPTS=<this file>`; see sign_windows.ps1.

if(NOT IS_DIRECTORY "${CPACK_TEMPORARY_DIRECTORY}")
  message(FATAL_ERROR "cpack_sign.cmake: no staging folder in CPACK_TEMPORARY_DIRECTORY ('${CPACK_TEMPORARY_DIRECTORY}')")
endif()

execute_process(
  COMMAND pwsh -NoProfile -File "${CMAKE_CURRENT_LIST_DIR}/sign_windows.ps1" "${CPACK_TEMPORARY_DIRECTORY}"
  RESULT_VARIABLE result
)
if(NOT result EQUAL 0)
  message(FATAL_ERROR "cpack_sign.cmake: signing the staged files failed (exit ${result})")
endif()
