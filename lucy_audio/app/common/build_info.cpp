#include "build_info.h"

#include "build_info_config.h"

#include <ostream>
#include <string>

namespace minitts::app {
namespace {

std::string build_type() {
    std::string value = STELNET_AUDIO_BUILD_TYPE_STRING;
    if (value.empty()) {
        value = "unknown";
    }
    return value;
}

std::string compiler_name() {
    const std::string id = STELNET_AUDIO_COMPILER_ID_STRING;
    const std::string version = STELNET_AUDIO_COMPILER_VERSION_STRING;
    if (id == "GNU") {
        return "gcc " + version;
    }
    if (id == "Clang") {
        return "clang " + version;
    }
    if (id == "AppleClang") {
        return "apple-clang " + version;
    }
    if (id == "MSVC") {
        return "msvc " + version;
    }
    return id.empty() ? "unknown" : id + " " + version;
}

}  // namespace

void print_build_info(std::ostream & out) {
    out << "lucy_audio " << STELNET_AUDIO_VERSION_STRING << "\n"
        << "git: " << STELNET_AUDIO_GIT_SHA_STRING << " " << STELNET_AUDIO_GIT_DATE_STRING << "\n"
        << "build: " << build_type() << ", " << compiler_name() << ",\n"
        << STELNET_AUDIO_SYSTEM_NAME_STRING << " " << STELNET_AUDIO_SYSTEM_PROCESSOR_STRING << "\n"
        << "backends: " << STELNET_AUDIO_BUILD_BACKENDS_STRING << "\n";
}

void print_build_info_summary(std::ostream & out) {
    out << "lucy_audio " << STELNET_AUDIO_VERSION_STRING
        << " (git " << STELNET_AUDIO_GIT_SHA_STRING << ", " << build_type() << ")"
        << " [backends: " << STELNET_AUDIO_BUILD_BACKENDS_STRING << "]\n";
}

}  // namespace minitts::app
