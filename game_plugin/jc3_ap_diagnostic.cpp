#define WIN32_LEAN_AND_MEAN
#include <Windows.h>
#include <TlHelp32.h>

#include <cstdarg>
#include <cstdio>
#include <string>

namespace {

constexpr wchar_t kLogFileName[] = L"jc3_ap_diagnostic.log";
constexpr int kHotkey = VK_F8;

std::wstring GetGameDirectory() {
    wchar_t path[MAX_PATH]{};
    const DWORD length = GetModuleFileNameW(nullptr, path, MAX_PATH);
    if (length == 0 || length >= MAX_PATH) {
        return L".";
    }

    std::wstring directory(path, length);
    const std::size_t separator = directory.find_last_of(L"\\/");
    if (separator == std::wstring::npos) {
        return L".";
    }
    directory.resize(separator);
    return directory;
}

void Log(const wchar_t* format, ...) {
    wchar_t message[1024]{};
    va_list args;
    va_start(args, format);
    const int message_length = _vsnwprintf_s(
        message, _countof(message), _TRUNCATE, format, args);
    va_end(args);
    if (message_length < 0) {
        return;
    }

    SYSTEMTIME now{};
    GetLocalTime(&now);
    wchar_t line[1200]{};
    const int line_length = swprintf_s(
        line,
        L"[%04u-%02u-%02u %02u:%02u:%02u] %ls\r\n",
        now.wYear,
        now.wMonth,
        now.wDay,
        now.wHour,
        now.wMinute,
        now.wSecond,
        message);
    if (line_length <= 0) {
        return;
    }

    const std::wstring path = GetGameDirectory() + L"\\" + kLogFileName;
    HANDLE file = CreateFileW(
        path.c_str(),
        FILE_APPEND_DATA,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        nullptr,
        OPEN_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
    if (file == INVALID_HANDLE_VALUE) {
        OutputDebugStringW(L"JC3 AP diagnostic: could not open log file.\n");
        return;
    }

    LARGE_INTEGER size{};
    if (GetFileSizeEx(file, &size) && size.QuadPart == 0) {
        constexpr wchar_t bom = 0xFEFF;
        DWORD written = 0;
        WriteFile(file, &bom, sizeof(bom), &written, nullptr);
    }

    DWORD written = 0;
    WriteFile(
        file,
        line,
        static_cast<DWORD>(line_length * sizeof(wchar_t)),
        &written,
        nullptr);
    CloseHandle(file);
}

void DumpModuleMap() {
    HANDLE snapshot = CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, GetCurrentProcessId());
    if (snapshot == INVALID_HANDLE_VALUE) {
        Log(L"Module snapshot failed (error %lu).", GetLastError());
        return;
    }

    Log(L"--- Loaded modules (base address and image size) ---");
    MODULEENTRY32W module{};
    module.dwSize = sizeof(module);
    if (Module32FirstW(snapshot, &module)) {
        do {
            Log(
                L"%ls | base=%p | image_size=%lu bytes",
                module.szModule,
                static_cast<void*>(module.modBaseAddr),
                module.modBaseSize);
            module.dwSize = sizeof(module);
        } while (Module32NextW(snapshot, &module));
    } else {
        Log(L"Could not enumerate modules (error %lu).", GetLastError());
    }
    CloseHandle(snapshot);
    Log(L"--- End module map ---");
}

DWORD WINAPI DiagnosticThread(void*) {
    Log(L"JC3 AP diagnostic plugin loaded. Press F8 to export loaded-module addresses.");
    DumpModuleMap();

    bool was_down = false;
    for (;;) {
        const bool is_down = (GetAsyncKeyState(kHotkey) & 0x8000) != 0;
        if (is_down && !was_down) {
            DumpModuleMap();
        }
        was_down = is_down;
        Sleep(100);
    }
}

}  // namespace

BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(module);
        HANDLE thread = CreateThread(nullptr, 0, DiagnosticThread, nullptr, 0, nullptr);
        if (thread) {
            CloseHandle(thread);
        } else {
            OutputDebugStringW(L"JC3 AP diagnostic: could not start worker thread.\n");
        }
    }
    return TRUE;
}
