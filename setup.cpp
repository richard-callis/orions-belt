/*
 * Orion's Belt — Portable Windows Setup + Launcher
 *
 * First run : installs venv, all packages, and downloads AI models (~670 MB),
 *             then automatically launches the app.
 * Every run after that : detects the completed setup and launches immediately.
 *
 * No installation required. No admin rights needed.
 * Requires Python 3.11+ on PATH — everything else is handled here.
 *
 * Build — fully static (runs on any Windows 10+ machine, zero runtime deps):
 *
 *   MSVC (Developer Command Prompt):
 *     cl /EHsc /O2 /utf-8 /MT setup.cpp winhttp.lib /Fe:setup.exe
 *
 *   MinGW / MSYS2:
 *     g++ -O2 -static -o setup.exe setup.cpp -lwinhttp -municode
 *
 * Or push a "v*" tag — GitHub Actions builds and attaches it to the release.
 */

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <winhttp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <string>
#include <vector>
#include <algorithm>
#include <stdint.h>

#pragma comment(lib, "winhttp.lib")

// ── ANSI / VT100 colour codes (Win10+ console supports these natively) ────────
#define C_RESET   "\x1b[0m"
#define C_BOLD    "\x1b[1m"
#define C_RED     "\x1b[31m"
#define C_GREEN   "\x1b[32m"
#define C_YELLOW  "\x1b[33m"
#define C_CYAN    "\x1b[36m"
#define C_WHITE   "\x1b[97m"
#define C_GRAY    "\x1b[90m"

// ── Enable VT processing once at start ───────────────────────────────────────
static void enable_vt() {
    HANDLE h = GetStdHandle(STD_OUTPUT_HANDLE);
    DWORD mode = 0;
    if (GetConsoleMode(h, &mode))
        SetConsoleMode(h, mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING
                                | DISABLE_NEWLINE_AUTO_RETURN);
    SetConsoleOutputCP(CP_UTF8);
}

// ── Change working directory to wherever this exe lives ───────────────────────
// Means the exe works correctly whether double-clicked, dragged to a terminal,
// or launched from a different directory.
static void cd_to_exe_dir() {
    char path[MAX_PATH];
    GetModuleFileNameA(nullptr, path, MAX_PATH);
    char* last = strrchr(path, '\\');
    if (last) { *last = '\0'; SetCurrentDirectoryA(path); }
}

// ── Setup completion marker ───────────────────────────────────────────────────
static const char* DONE_MARKER = ".setup_complete";

static bool setup_is_done() {
    // We consider setup done when both the venv and the completion marker exist.
    // The marker is written at the very end of the first run.
    return GetFileAttributesA(DONE_MARKER) != INVALID_FILE_ATTRIBUTES
        && GetFileAttributesA(".venv")      != INVALID_FILE_ATTRIBUTES;
}

static void write_done_marker() {
    HANDLE h = CreateFileA(DONE_MARKER, GENERIC_WRITE, 0, nullptr,
                            CREATE_ALWAYS, FILE_ATTRIBUTE_HIDDEN, nullptr);
    if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
}

// ── Step table ────────────────────────────────────────────────────────────────
struct Step { const char* label; const char* est; };
static const Step STEPS[] = {
    { "Check Python 3.11+",              "~2s"       },
    { "Create virtual environment",      "~10s"      },
    { "Upgrade pip",                     "~15s"      },
    { "Install core dependencies",       "~1-3 min"  },
    { "Install PyTorch (CPU build)",     "~5-15 min" },
    { "Install NLP stack",               "~2-5 min"  },
    { "Install desktop launcher",        "~30s"      },
    { "Download AI models (~670 MB)",    "~5-30 min" },
};
static const int N_STEPS = (int)(sizeof(STEPS) / sizeof(STEPS[0]));

// ── UI helpers ────────────────────────────────────────────────────────────────
static void print_setup_banner() {
    printf(C_BOLD C_WHITE
           "\n"
           "  \xe2\x95\x94\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x97\n"
           "  \xe2\x95\x91     Orion's Belt  \xe2\x80\x94  First-time Setup          \xe2\x95\x91\n"
           "  \xe2\x95\x9a\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x90\xe2\x95\x9d\n"
           C_RESET "\n");

    printf("  This only runs once. Estimated time: "
           C_YELLOW "10\xe2\x80\x94" "40 min" C_RESET
           "  (network speed varies)\n\n");

    printf("  Steps:\n");
    for (int i = 0; i < N_STEPS; i++)
        printf("    " C_GRAY "[%d/%d]" C_RESET "  %-36s " C_GRAY "%s" C_RESET "\n",
               i + 1, N_STEPS, STEPS[i].label, STEPS[i].est);
    printf("\n");
}

static void step_begin(int step /* 1-based */) {
    printf("\n  " C_BOLD C_CYAN "[%d/%d]" C_RESET "  " C_WHITE "%s" C_RESET "\n",
           step, N_STEPS, STEPS[step - 1].label);
}

static void step_ok(const char* detail = nullptr) {
    if (detail) printf("        " C_GREEN "\xe2\x9c\x93" C_RESET "  %s\n", detail);
    else        printf("        " C_GREEN "\xe2\x9c\x93  Done" C_RESET "\n");
}

static void step_warn(const char* msg) {
    printf("        " C_YELLOW "\xe2\x9a\xa0" C_RESET "  %s\n", msg);
}

static void step_fail(const char* msg) {
    printf("\n  " C_RED "\xe2\x9c\x97  FAILED:" C_RESET "  %s\n\n", msg);
}

static void pause_on_error() {
    printf("  Press Enter to close...");
    fflush(stdout);
    getchar();
}

// ── Progress bar ──────────────────────────────────────────────────────────────
static void draw_bar(int64_t done, int64_t total, double bps) {
    if (total <= 0) return;
    const int W = 38;
    int filled = (int)((done * W) / total);
    int pct    = (int)((done * 100LL) / total);

    printf("\r  [");
    for (int i = 0; i < W; i++)
        printf(i < filled ? "\xe2\x96\x88" : "\xe2\x96\x91");
    printf("] " C_WHITE "%3d%%" C_RESET, pct);

    if (bps > 0) {
        double speed_mb = bps / (1024.0 * 1024.0);
        int64_t rem     = total - done;
        double  eta_s   = rem / bps;

        if (speed_mb >= 1.0) printf("  " C_GRAY "%.1f MB/s", speed_mb);
        else                  printf("  " C_GRAY "%.0f KB/s", bps / 1024.0);

        if      (eta_s > 60) printf("  ~%.0fm left" C_RESET, eta_s / 60.0);
        else if (eta_s >  0) printf("  ~%.0fs left" C_RESET, eta_s);
        else                 printf(C_RESET);
    }
    fflush(stdout);
}

// ── Subprocess helpers ────────────────────────────────────────────────────────
// Streams child stdout+stderr straight to our console.
static int run(const std::string& cmd) {
    STARTUPINFOA si = {}; si.cb = sizeof(si);
    PROCESS_INFORMATION pi = {};
    std::string mut = cmd;
    if (!CreateProcessA(nullptr, &mut[0], nullptr, nullptr, TRUE,
                        0, nullptr, nullptr, &si, &pi))
        return -1;
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess); CloseHandle(pi.hThread);
    return (int)code;
}

// Captures child output silently.
static int run_capture(const std::string& cmd, std::string& out) {
    SECURITY_ATTRIBUTES sa = {}; sa.nLength = sizeof(sa); sa.bInheritHandle = TRUE;
    HANDLE hr = nullptr, hw = nullptr;
    CreatePipe(&hr, &hw, &sa, 0);
    SetHandleInformation(hr, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOA si = {}; si.cb = sizeof(si);
    si.dwFlags    = STARTF_USESTDHANDLES;
    si.hStdOutput = hw; si.hStdError = hw;
    si.hStdInput  = GetStdHandle(STD_INPUT_HANDLE);

    PROCESS_INFORMATION pi = {};
    std::string mut = cmd;
    if (!CreateProcessA(nullptr, &mut[0], nullptr, nullptr, TRUE,
                        0, nullptr, nullptr, &si, &pi)) {
        CloseHandle(hr); CloseHandle(hw); return -1;
    }
    CloseHandle(hw);

    char buf[4096]; DWORD bytes;
    while (ReadFile(hr, buf, sizeof(buf)-1, &bytes, nullptr) && bytes)
        out.append(buf, bytes);
    CloseHandle(hr);

    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess); CloseHandle(pi.hThread);
    return (int)code;
}

// ── Python detection ──────────────────────────────────────────────────────────
static std::string find_python() {
    const char* candidates[] = { "python", "python3", "py" };
    for (auto name : candidates) {
        std::string out;
        if (run_capture(std::string(name) + " --version", out) == 0) {
            int major = 0, minor = 0;
            if (sscanf(out.c_str(), "Python %d.%d", &major, &minor) == 2
                    && major == 3 && minor >= 11)
                return name;
        }
    }
    return "";
}

// ── HuggingFace model download ────────────────────────────────────────────────
struct Model { const char* id; int64_t approx_mb; const char* purpose; };

static const Model MODELS[] = {
    { "urchade/gliner_medium-v2.1",              400,
      "Zero-shot NER \xe2\x80\x94 detects PII/PHI" },
    { "cross-encoder/nli-deberta-v3-small",      180,
      "PHI judge \xe2\x80\x94 classifies ambiguous text" },
    { "sentence-transformers/all-MiniLM-L6-v2",   90,
      "Memory embeddings \xe2\x80\x94 similarity recall" },
};
static const int N_MODELS = (int)(sizeof(MODELS) / sizeof(MODELS[0]));

static std::vector<std::string> json_strings(const std::string& json,
                                              const std::string& key) {
    std::vector<std::string> result;
    std::string needle = "\"" + key + "\"";
    size_t pos = 0;
    while ((pos = json.find(needle, pos)) != std::string::npos) {
        pos = json.find(':', pos); if (pos == std::string::npos) break;
        pos = json.find('"', pos); if (pos == std::string::npos) break;
        pos++;
        size_t end = pos;
        while (end < json.size() && json[end] != '"') {
            if (json[end] == '\\') end++;
            end++;
        }
        result.push_back(json.substr(pos, end - pos));
        pos = end + 1;
    }
    return result;
}

static HINTERNET g_session = nullptr;

static void http_init() {
    g_session = WinHttpOpen(L"OrionsBelt-Setup/1.0",
                            WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
                            WINHTTP_NO_PROXY_NAME,
                            WINHTTP_NO_PROXY_BYPASS, 0);
}

static std::string https_get(const std::string& host_u8,
                              const std::string& path_u8) {
    if (!g_session) return "";
    auto to_wide = [](const std::string& s) {
        int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
        std::wstring w(n, 0);
        MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, &w[0], n);
        return w;
    };
    HINTERNET hConn = WinHttpConnect(g_session, to_wide(host_u8).c_str(),
                                     INTERNET_DEFAULT_HTTPS_PORT, 0);
    if (!hConn) return "";
    HINTERNET hReq  = WinHttpOpenRequest(hConn, L"GET", to_wide(path_u8).c_str(),
                                         nullptr, WINHTTP_NO_REFERER,
                                         WINHTTP_DEFAULT_ACCEPT_TYPES,
                                         WINHTTP_FLAG_SECURE);
    if (!hReq) { WinHttpCloseHandle(hConn); return ""; }
    DWORD redir = WINHTTP_OPTION_REDIRECT_POLICY_ALWAYS;
    WinHttpSetOption(hReq, WINHTTP_OPTION_REDIRECT_POLICY, &redir, sizeof(redir));
    if (!WinHttpSendRequest(hReq, WINHTTP_NO_ADDITIONAL_HEADERS, 0,
                            WINHTTP_NO_REQUEST_DATA, 0, 0, 0) ||
        !WinHttpReceiveResponse(hReq, nullptr)) {
        WinHttpCloseHandle(hReq); WinHttpCloseHandle(hConn); return "";
    }
    std::string body; DWORD avail; char buf[65536];
    while (WinHttpQueryDataAvailable(hReq, &avail) && avail > 0) {
        DWORD got, chunk = (avail < sizeof(buf)) ? avail : (DWORD)sizeof(buf);
        if (!WinHttpReadData(hReq, buf, chunk, &got) || !got) break;
        body.append(buf, got);
    }
    WinHttpCloseHandle(hReq); WinHttpCloseHandle(hConn);
    return body;
}

static void mkdirs(const std::string& dir) {
    std::string p = dir;
    for (size_t i = 1; i < p.size(); i++) {
        if (p[i] == '\\' || p[i] == '/') {
            char c = p[i]; p[i] = '\0';
            CreateDirectoryA(p.c_str(), nullptr);
            p[i] = c;
        }
    }
    CreateDirectoryA(p.c_str(), nullptr);
}

static bool model_is_cached(const std::string& model_id) {
    size_t slash = model_id.find('/');
    if (slash == std::string::npos) return false;
    std::string marker = "models\\hub\\models--"
        + model_id.substr(0, slash) + "--"
        + model_id.substr(slash + 1)
        + "\\refs\\main";
    return GetFileAttributesA(marker.c_str()) != INVALID_FILE_ATTRIBUTES;
}

static bool download_file(const std::string& model_id,
                           const std::string& filename,
                           const std::string& local_path,
                           int64_t& session_done, int64_t session_total) {
    auto to_wide = [](const std::string& s) {
        int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
        std::wstring w(n, 0);
        MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, &w[0], n);
        return w;
    };
    std::string path = "/" + model_id + "/resolve/main/" + filename;
    HINTERNET hConn = WinHttpConnect(g_session, L"huggingface.co",
                                     INTERNET_DEFAULT_HTTPS_PORT, 0);
    if (!hConn) return false;
    HINTERNET hReq  = WinHttpOpenRequest(hConn, L"GET", to_wide(path).c_str(),
                                         nullptr, WINHTTP_NO_REFERER,
                                         WINHTTP_DEFAULT_ACCEPT_TYPES,
                                         WINHTTP_FLAG_SECURE);
    if (!hReq) { WinHttpCloseHandle(hConn); return false; }
    DWORD redir = WINHTTP_OPTION_REDIRECT_POLICY_ALWAYS;
    WinHttpSetOption(hReq, WINHTTP_OPTION_REDIRECT_POLICY, &redir, sizeof(redir));
    if (!WinHttpSendRequest(hReq, WINHTTP_NO_ADDITIONAL_HEADERS, 0,
                            WINHTTP_NO_REQUEST_DATA, 0, 0, 0) ||
        !WinHttpReceiveResponse(hReq, nullptr)) {
        WinHttpCloseHandle(hReq); WinHttpCloseHandle(hConn); return false;
    }
    int64_t file_size = 0;
    {
        wchar_t cl_buf[64] = {}; DWORD cl_len = sizeof(cl_buf), cl_idx = 0;
        if (WinHttpQueryHeaders(hReq, WINHTTP_QUERY_CONTENT_LENGTH,
                                WINHTTP_HEADER_NAME_BY_INDEX,
                                cl_buf, &cl_len, &cl_idx))
            file_size = _wtoi64(cl_buf);
    }
    HANDLE hf = CreateFileA(local_path.c_str(), GENERIC_WRITE, 0, nullptr,
                             CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (hf == INVALID_HANDLE_VALUE) {
        WinHttpCloseHandle(hReq); WinHttpCloseHandle(hConn); return false;
    }
    int64_t file_done = 0;
    LARGE_INTEGER freq, t0, t1;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&t0);
    DWORD avail; char buf[131072]; bool ok = true;
    while (WinHttpQueryDataAvailable(hReq, &avail) && avail > 0) {
        DWORD chunk = (avail < sizeof(buf)) ? avail : (DWORD)sizeof(buf), got;
        if (!WinHttpReadData(hReq, buf, chunk, &got) || !got) { ok = false; break; }
        DWORD written; WriteFile(hf, buf, got, &written, nullptr);
        file_done += got; session_done += got;
        QueryPerformanceCounter(&t1);
        double elapsed = (double)(t1.QuadPart - t0.QuadPart) / freq.QuadPart;
        draw_bar(session_done, session_total, elapsed > 0 ? session_done / elapsed : 0);
    }
    CloseHandle(hf);
    WinHttpCloseHandle(hReq); WinHttpCloseHandle(hConn);
    if (!ok || (file_size > 0 && file_done < file_size)) {
        DeleteFileA(local_path.c_str()); return false;
    }
    return true;
}

static bool download_model(int idx) {
    const Model& m = MODELS[idx];
    printf("\n       " C_CYAN "%s" C_RESET "  (%lld MB)\n", m.id, m.approx_mb);
    printf("       %s\n\n", m.purpose);
    if (model_is_cached(m.id)) {
        printf("       " C_GREEN "\xe2\x9c\x93  Already cached \xe2\x80\x94 skipping" C_RESET "\n");
        return true;
    }
    std::string api_body = https_get("huggingface.co",
                                      "/api/models/" + std::string(m.id));
    if (api_body.empty()) {
        printf("       " C_RED "\xe2\x9c\x97  Could not reach huggingface.co" C_RESET "\n");
        return false;
    }
    auto filenames = json_strings(api_body, "rfilename");
    if (filenames.empty()) {
        printf("       " C_RED "\xe2\x9c\x97  Could not parse model file list" C_RESET "\n");
        return false;
    }
    int64_t total_bytes = m.approx_mb * 1024LL * 1024LL, session_done = 0;
    size_t slash = std::string(m.id).find('/');
    std::string cache_base = "models\\hub\\models--"
        + std::string(m.id).substr(0, slash) + "--"
        + std::string(m.id).substr(slash + 1);
    std::string snap_dir = cache_base + "\\snapshots\\main";
    mkdirs(snap_dir);
    bool all_ok = true;
    int n = (int)filenames.size();
    for (int fi = 0; fi < n; fi++) {
        const std::string& fname = filenames[fi];
        std::string local = snap_dir + "\\" + fname;
        size_t last = local.find_last_of("/\\");
        if (last != std::string::npos) mkdirs(local.substr(0, last));
        if (GetFileAttributesA(local.c_str()) != INVALID_FILE_ATTRIBUTES) {
            printf("       " C_GRAY "[%d/%d] %s \xe2\x80\x94 cached" C_RESET "\n",
                   fi+1, n, fname.c_str());
            continue;
        }
        printf("       " C_GRAY "[%d/%d] %s" C_RESET "\n", fi+1, n, fname.c_str());
        if (!download_file(m.id, fname, local, session_done, total_bytes)) {
            printf("\n       " C_RED "\xe2\x9c\x97  Failed: %s" C_RESET "\n", fname.c_str());
            all_ok = false;
        }
    }
    printf("\n");
    if (all_ok) {
        std::string refs = cache_base + "\\refs";
        mkdirs(refs);
        HANDLE h = CreateFileA((refs + "\\main").c_str(), GENERIC_WRITE, 0,
                                nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (h != INVALID_HANDLE_VALUE) {
            DWORD w; WriteFile(h, "main\n", 5, &w, nullptr); CloseHandle(h);
        }
    }
    return all_ok;
}

// ── App launcher ──────────────────────────────────────────────────────────────
// Launches the app and waits for it to exit (so this console stays alive until
// the user closes the app window, then both close together).
static int launch_app() {
    printf("\n  " C_BOLD C_GREEN
           "=========================================\n"
           "   Setup complete!  Launching app...\n"
           "=========================================" C_RESET "\n\n");
    fflush(stdout);
    // Small pause so the user sees the success message before the app opens
    Sleep(800);
    return run(".venv\\Scripts\\python launch.py");
}

// ── Main ──────────────────────────────────────────────────────────────────────
int main() {
    enable_vt();
    cd_to_exe_dir();

    // ── Already set up — just launch ─────────────────────────────────────────
    if (setup_is_done()) {
        printf(C_BOLD C_WHITE "\n  Orion's Belt" C_RESET "\n");
        printf(C_GRAY "  Setup already complete. Starting...\n\n" C_RESET);
        fflush(stdout);
        return run(".venv\\Scripts\\python launch.py");
    }

    // ── First-time setup ─────────────────────────────────────────────────────
    print_setup_banner();

    // 1. Check Python
    step_begin(1);
    std::string python = find_python();
    if (python.empty()) {
        step_fail("Python 3.11+ not found on PATH.\n"
                  "         Install from https://python.org and ensure it is on PATH.");
        pause_on_error(); return 1;
    }
    {
        std::string out; run_capture(python + " --version", out);
        while (!out.empty() && (out.back()=='\n'||out.back()=='\r')) out.pop_back();
        step_ok(out.c_str());
    }

    // 2. Create venv
    step_begin(2);
    if (GetFileAttributesA(".venv") != INVALID_FILE_ATTRIBUTES) {
        step_ok(".venv already exists \xe2\x80\x94 reusing");
    } else {
        if (run(python + " -m venv .venv") != 0) {
            step_fail("Could not create .venv");
            pause_on_error(); return 1;
        }
        step_ok();
    }

    std::string pip = ".venv\\Scripts\\pip";
    std::string py  = ".venv\\Scripts\\python";

    // 3. Upgrade pip
    step_begin(3);
    run(py + " -m pip install --upgrade pip --quiet");
    step_ok();

    // 4. Core dependencies
    step_begin(4);
    if (run(pip + " install -r requirements.txt") != 0) {
        step_fail("Core dependency install failed");
        pause_on_error(); return 1;
    }
    step_ok();

    // 5. PyTorch CPU build
    step_begin(5);
    printf("       (Largest download ~800 MB.  Grab a coffee.)\n\n");
    int r = run(py + " -m pip install --force-reinstall \"torch==2.7.1+cpu\""
                " --index-url https://download.pytorch.org/whl/cpu");
    if (r != 0) {
        step_warn("Retrying with trusted-host bypass (corporate proxy?)...");
        r = run(py + " -m pip install --force-reinstall \"torch==2.7.1+cpu\""
                " --index-url https://download.pytorch.org/whl/cpu"
                " --trusted-host download.pytorch.org"
                " --trusted-host files.pythonhosted.org");
    }
    if (r != 0) step_warn("PyTorch install failed. PII Guard stages 2+3 disabled.");
    else        step_ok();

    // 6. NLP stack
    step_begin(6);
    run(pip + " install transformers sentence-transformers numpy --quiet");
    run(pip + " install presidio-analyzer presidio-anonymizer spacy --quiet");
    if (run(pip + " install gliner protobuf --quiet") != 0)
        step_warn("GLiNER install failed. Stage 2 NER disabled.");
    run(py + " install_spacy_model.py");
    step_ok();

    // 7. Desktop launcher
    step_begin(7);
    run(pip + " install pywebview pystray --quiet");
    if (run(pip + " install pywin32 pyodbc --quiet") != 0)
        step_warn("pywin32/pyodbc optional \xe2\x80\x94 Outlook and SQL Server disabled.");
    else
        step_ok();

    // 8. Download AI models
    step_begin(8);
    printf("       gliner_medium-v2.1       ~400 MB   PII detection\n");
    printf("       nli-deberta-v3-small     ~180 MB   PHI judge\n");
    printf("       all-MiniLM-L6-v2          ~90 MB   Memory embeddings\n");
    CreateDirectoryA("models", nullptr);
    CreateDirectoryA("logs",   nullptr);
    http_init();
    bool any_failed = false;
    for (int i = 0; i < N_MODELS; i++)
        if (!download_model(i)) { any_failed = true; step_warn("Some files failed \xe2\x80\x94 the app will retry on first use."); }
    if (!any_failed) step_ok("All models cached");

    // ── Mark setup done so future runs skip straight to launch ───────────────
    write_done_marker();

    // ── Launch ────────────────────────────────────────────────────────────────
    return launch_app();
}
