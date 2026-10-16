set(STELNET_AUDIO_EXTERNAL_SERVER_FRONTENDS_SOURCE_DIR "${CMAKE_CURRENT_LIST_DIR}")

function(stelnet_audio_external_frontend_requested STELNET_AUDIO_EXTERNAL_FRONTEND_NAME STELNET_AUDIO_EXTERNAL_FRONTEND_OUT)
    set(STELNET_AUDIO_EXTERNAL_FRONTEND_FOUND OFF)
    foreach(STELNET_AUDIO_EXTERNAL_FRONTEND_MODULE IN LISTS STELNET_AUDIO_SERVER_FRONTEND_MODULES)
        if (STELNET_AUDIO_EXTERNAL_FRONTEND_MODULE STREQUAL "${STELNET_AUDIO_EXTERNAL_FRONTEND_NAME}")
            set(STELNET_AUDIO_EXTERNAL_FRONTEND_FOUND ON)
        endif()
    endforeach()
    set(${STELNET_AUDIO_EXTERNAL_FRONTEND_OUT} ${STELNET_AUDIO_EXTERNAL_FRONTEND_FOUND} PARENT_SCOPE)
endfunction()

function(stelnet_audio_external_frontend_require_cpp_httplib)
    if (TARGET stelnet_audio_cpp_httplib)
        return()
    endif()
    add_subdirectory(
        "${PROJECT_SOURCE_DIR}/external/cpp-httplib"
        "${CMAKE_CURRENT_BINARY_DIR}/audio.cpp-server-frontends/cpp-httplib")
endfunction()

stelnet_audio_external_frontend_requested(audio_decode STELNET_AUDIO_EXTERNAL_FRONTEND_AUDIO_DECODE)
if (STELNET_AUDIO_EXTERNAL_FRONTEND_AUDIO_DECODE)
    stelnet_audio_register_server_frontend(audio_decode
        REGISTER_FUNCTION register_audio_decode_module
        SOURCES
            "${STELNET_AUDIO_EXTERNAL_SERVER_FRONTENDS_SOURCE_DIR}/modules/audio_decode.cpp"
        INCLUDE_DIRS
            "${STELNET_AUDIO_EXTERNAL_SERVER_FRONTENDS_SOURCE_DIR}/modules"
            "${STELNET_AUDIO_EXTERNAL_SERVER_FRONTENDS_SOURCE_DIR}/external/miniaudio")
endif()

stelnet_audio_external_frontend_requested(mp3_encode STELNET_AUDIO_EXTERNAL_FRONTEND_MP3_ENCODE)
if (STELNET_AUDIO_EXTERNAL_FRONTEND_MP3_ENCODE)
    set(STELNET_AUDIO_LAME_ROOT ""
        CACHE PATH "Optional libmp3lame install prefix for the server mp3_encode frontend")
    find_path(STELNET_AUDIO_LAME_INCLUDE_DIR
        NAMES lame/lame.h
        HINTS "${STELNET_AUDIO_LAME_ROOT}" "$ENV{CONDA_PREFIX}"
        PATH_SUFFIXES include Library/include)
    find_library(STELNET_AUDIO_LAME_LIBRARY
        NAMES mp3lame lame
        HINTS "${STELNET_AUDIO_LAME_ROOT}" "$ENV{CONDA_PREFIX}"
        PATH_SUFFIXES lib Library/lib)
    if (NOT STELNET_AUDIO_LAME_INCLUDE_DIR OR NOT STELNET_AUDIO_LAME_LIBRARY)
        message(FATAL_ERROR
            "STELNET_AUDIO_SERVER_FRONTEND_MODULES=mp3_encode requires libmp3lame headers and library "
            "(lame/lame.h and libmp3lame). Install libmp3lame with your package manager, "
            "use a conda environment that provides lame, or pass -DSTELNET_AUDIO_LAME_ROOT=<prefix>.")
    endif()
    stelnet_audio_register_server_frontend(mp3_encode
        REGISTER_FUNCTION register_mp3_encode_module
        SOURCES
            "${STELNET_AUDIO_EXTERNAL_SERVER_FRONTENDS_SOURCE_DIR}/modules/mp3_encode.cpp"
        INCLUDE_DIRS
            "${STELNET_AUDIO_EXTERNAL_SERVER_FRONTENDS_SOURCE_DIR}/modules"
            "${STELNET_AUDIO_LAME_INCLUDE_DIR}"
        LIBRARIES
            "${STELNET_AUDIO_LAME_LIBRARY}")
endif()

stelnet_audio_external_frontend_requested(https STELNET_AUDIO_EXTERNAL_FRONTEND_HTTPS)
if (STELNET_AUDIO_EXTERNAL_FRONTEND_HTTPS)
    stelnet_audio_external_frontend_require_cpp_httplib()
    stelnet_audio_register_server_frontend(https
        REGISTER_FUNCTION register_https_listener
        SOURCES
            "${STELNET_AUDIO_EXTERNAL_SERVER_FRONTENDS_SOURCE_DIR}/modules/https.cpp"
        INCLUDE_DIRS
            "${STELNET_AUDIO_EXTERNAL_SERVER_FRONTENDS_SOURCE_DIR}/modules"
        LIBRARIES
            stelnet_audio_cpp_httplib)
endif()

stelnet_audio_external_frontend_requested(websocket STELNET_AUDIO_EXTERNAL_FRONTEND_WEBSOCKET)
if (STELNET_AUDIO_EXTERNAL_FRONTEND_WEBSOCKET)
    stelnet_audio_external_frontend_require_cpp_httplib()
    stelnet_audio_register_server_frontend(websocket
        REGISTER_FUNCTION register_websocket_listener
        SOURCES
            "${STELNET_AUDIO_EXTERNAL_SERVER_FRONTENDS_SOURCE_DIR}/modules/websocket.cpp"
        INCLUDE_DIRS
            "${STELNET_AUDIO_EXTERNAL_SERVER_FRONTENDS_SOURCE_DIR}/modules"
        LIBRARIES
            stelnet_audio_cpp_httplib)
endif()
