#include <atomic>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <thread>

#ifdef _WIN32
#include <windows.h>
#else
#include <dlfcn.h>
#endif

namespace {

using TdClient = void *;
using TdJsonClientCreate = TdClient (*)();
using TdJsonClientSend = void (*)(TdClient, const char *request);
using TdJsonClientReceive = const char *(*)(TdClient, double timeout);
using TdJsonClientExecute = const char *(*)(TdClient, const char *request);
using TdJsonClientDestroy = void (*)(TdClient);

struct TdJsonApi {
    TdJsonClientCreate create = nullptr;
    TdJsonClientSend send = nullptr;
    TdJsonClientReceive receive = nullptr;
    TdJsonClientExecute execute = nullptr;
    TdJsonClientDestroy destroy = nullptr;

#ifdef _WIN32
    HMODULE module = nullptr;
#else
    void *module = nullptr;
#endif

    ~TdJsonApi() {
#ifdef _WIN32
        if (module != nullptr) {
            FreeLibrary(module);
        }
#else
        if (module != nullptr) {
            dlclose(module);
        }
#endif
    }

    TdJsonApi(const TdJsonApi &) = delete;
    TdJsonApi &operator=(const TdJsonApi &) = delete;

    TdJsonApi() = default;

    bool load(const std::filesystem::path &path, std::string &error) {
#ifdef _WIN32
        // Resolve TDLib's OpenSSL/zlib DLLs relative to tdjson.dll, not the
        // caller's working directory.
        module = LoadLibraryExW(
            path.wstring().c_str(),
            nullptr,
            LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS
        );
        if (module == nullptr) {
            error = "LoadLibrary failed for " + path.string() + " (win32=" +
                    std::to_string(GetLastError()) + ")";
            return false;
        }
        create = reinterpret_cast<TdJsonClientCreate>(GetProcAddress(module, "td_json_client_create"));
        send = reinterpret_cast<TdJsonClientSend>(GetProcAddress(module, "td_json_client_send"));
        receive = reinterpret_cast<TdJsonClientReceive>(GetProcAddress(module, "td_json_client_receive"));
        execute = reinterpret_cast<TdJsonClientExecute>(GetProcAddress(module, "td_json_client_execute"));
        destroy = reinterpret_cast<TdJsonClientDestroy>(GetProcAddress(module, "td_json_client_destroy"));
#else
        module = dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL);
        if (module == nullptr) {
            error = "dlopen failed for " + path.string() + ": " + dlerror();
            return false;
        }
        create = reinterpret_cast<TdJsonClientCreate>(dlsym(module, "td_json_client_create"));
        send = reinterpret_cast<TdJsonClientSend>(dlsym(module, "td_json_client_send"));
        receive = reinterpret_cast<TdJsonClientReceive>(dlsym(module, "td_json_client_receive"));
        execute = reinterpret_cast<TdJsonClientExecute>(dlsym(module, "td_json_client_execute"));
        destroy = reinterpret_cast<TdJsonClientDestroy>(dlsym(module, "td_json_client_destroy"));
#endif
        if (create == nullptr || send == nullptr || receive == nullptr ||
            execute == nullptr || destroy == nullptr) {
            error = "tdjson library is missing one or more td_json_client_* symbols";
            return false;
        }
        return true;
    }
};

std::string json_error(std::string_view message) {
    std::string result = "{\"@type\":\"sidecar.error\",\"message\":\"";
    for (const char ch : message) {
        switch (ch) {
        case '\\':
            result += "\\\\";
            break;
        case '\"':
            result += "\\\"";
            break;
        case '\n':
            result += "\\n";
            break;
        case '\r':
            result += "\\r";
            break;
        case '\t':
            result += "\\t";
            break;
        default:
            result += ch;
            break;
        }
    }
    result += "\"}";
    return result;
}

bool is_control(std::string_view line, std::string_view type) {
    const std::string needle = "\"@type\":\"" + std::string(type) + "\"";
    return line.find(needle) != std::string_view::npos;
}

std::optional<std::filesystem::path> argument_value(
    int argc,
    char **argv,
    std::string_view name,
    std::string &error
) {
    for (int index = 1; index < argc; ++index) {
        if (argv[index] != nullptr && name == argv[index]) {
            if (index + 1 >= argc || argv[index + 1] == nullptr || argv[index + 1][0] == '\0') {
                error = "missing value for " + std::string(name);
                return std::nullopt;
            }
            return std::filesystem::path(argv[index + 1]);
        }
    }
    return std::filesystem::path();
}

void print_usage() {
    std::cout
        << "Neno TDLib sidecar\n"
        << "Reads one TDLib JSON request per stdin line and writes TDLib updates to stdout.\n"
        << "  --tdjson <path>  path to tdjson.dll (or set TDLIB_JSON_LIBRARY)\n"
        << "  --help           show this help\n"
        << "Control request: {\"@type\":\"sidecar.shutdown\"}\n"
        << std::flush;
}

} // namespace

int main(int argc, char **argv) {
    for (int index = 1; index < argc; ++index) {
        if (argv[index] == nullptr) {
            continue;
        }
        const std::string_view argument(argv[index]);
        if (argument == "--help" || argument == "-h") {
            print_usage();
            return 0;
        }
    }

    std::string argument_error;
    const auto argument_path = argument_value(argc, argv, "--tdjson", argument_error);
    if (!argument_error.empty()) {
        std::cerr << argument_error << '\n';
        return 2;
    }

    std::filesystem::path library_path;
    if (argument_path.has_value() && !argument_path->empty()) {
        library_path = *argument_path;
    } else if (const char *configured = std::getenv("TDLIB_JSON_LIBRARY"); configured != nullptr && configured[0] != '\0') {
        library_path = configured;
    } else {
#ifdef _WIN32
        library_path = "tdjson.dll";
#else
        library_path = "libtdjson.so";
#endif
    }

    TdJsonApi api;
    std::string load_error;
    if (!api.load(library_path, load_error)) {
        std::cerr << "sidecar: " << load_error << '\n';
        return 3;
    }

    const TdClient client = api.create();
    if (client == nullptr) {
        std::cerr << "sidecar: td_json_client_create returned null\n";
        return 4;
    }

    std::atomic<bool> stopping = false;
    std::mutex output_mutex;

    const auto emit = [&](const char *payload) {
        if (payload == nullptr || payload[0] == '\0') {
            return;
        }
        std::lock_guard<std::mutex> lock(output_mutex);
        std::cout << payload << '\n' << std::flush;
    };

    std::thread receiver([&]() {
        while (!stopping.load(std::memory_order_acquire)) {
            const char *payload = api.receive(client, 0.1);
            emit(payload);
        }
    });

    std::string line;
    while (std::getline(std::cin, line)) {
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        if (line.empty()) {
            continue;
        }
        if (is_control(line, "sidecar.shutdown")) {
            break;
        }
        api.send(client, line.c_str());
    }

    stopping.store(true, std::memory_order_release);
    receiver.join();
    api.destroy(client);
    return 0;
}
