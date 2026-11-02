/*
 * FISH — standalone HTTP server (Linux / container deployments).
 *
 * On a desktop the engine is loaded in-process by the Python server, so this
 * binary is optional. It exists for the case where FISH must run as its own
 * service: no Python, no runtime, one static binary.
 *
 * Endpoints
 *   GET  /fish/health   -> {"status":"ok"}
 *   GET  /fish/specs    -> measured hardware + execution plan
 *   GET  /stats         -> counters from the last generation
 *   POST /fish/stream   -> SSE: data: {"token":"..."} ... data: [DONE]
 *                          body: {"prompt": "...", "params": {...}}
 *                             or {"messages": [{"role","content"},...], "params": {...}}
 *
 * Security: binds 127.0.0.1 only, rejects non-loopback Host headers, and when
 * FISH_TOKEN is set every request must carry "Authorization: Bearer <token>".
 * A client that disconnects mid-stream cancels the generation.
 */
#include <fish/c_api.h>

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#ifdef _WIN32
#  include <winsock2.h>
#  include <ws2tcpip.h>
using sock_t = SOCKET;
#  define CLOSE_SOCK closesocket
#  define BAD_SOCK INVALID_SOCKET
#else
#  include <arpa/inet.h>
#  include <netinet/in.h>
#  include <signal.h>
#  include <sys/socket.h>
#  include <unistd.h>
using sock_t = int;
#  define CLOSE_SOCK ::close
#  define BAD_SOCK (-1)
#endif

namespace {

constexpr size_t kMaxBody = 1u << 20;
std::string g_token;  // FISH_TOKEN; empty = no auth

bool send_all(sock_t s, const char* data, size_t len) {
    while (len > 0) {
#ifdef _WIN32
        const int n = ::send(s, data, static_cast<int>(len), 0);
#else
        const ssize_t n = ::send(s, data, len, MSG_NOSIGNAL);
#endif
        if (n <= 0) return false;
        data += n;
        len -= static_cast<size_t>(n);
    }
    return true;
}

bool send_str(sock_t s, const std::string& text) { return send_all(s, text.data(), text.size()); }

std::string json_escape(const std::string& in) {
    std::string out;
    out.reserve(in.size() + 8);
    for (unsigned char c : in) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char esc[8];
                    std::snprintf(esc, sizeof(esc), "\\u%04x", c);
                    out += esc;
                } else {
                    out += static_cast<char>(c);
                }
        }
    }
    return out;
}

void append_utf8(std::string& out, uint32_t cp) {
    if (cp < 0x80) out += static_cast<char>(cp);
    else if (cp < 0x800) {
        out += static_cast<char>(0xC0 | (cp >> 6));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
        out += static_cast<char>(0xE0 | (cp >> 12));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else {
        out += static_cast<char>(0xF0 | (cp >> 18));
        out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    }
}

// Parse a JSON string literal starting at body[pos] == '"'. Handles every
// escape including \uXXXX surrogate pairs (Python's json.dumps emits them for
// anything outside ASCII). Returns the index after the closing quote.
size_t parse_json_string(const std::string& body, size_t pos, std::string* out) {
    std::string value;
    size_t i = pos + 1;
    while (i < body.size()) {
        const char c = body[i];
        if (c == '\\' && i + 1 < body.size()) {
            const char e = body[++i];
            switch (e) {
                case 'n': value += '\n'; break;
                case 'r': value += '\r'; break;
                case 't': value += '\t'; break;
                case 'b': value += '\b'; break;
                case 'f': value += '\f'; break;
                case '"': value += '"'; break;
                case '\\': value += '\\'; break;
                case '/': value += '/'; break;
                case 'u': {
                    if (i + 4 >= body.size()) return std::string::npos;
                    uint32_t cp = static_cast<uint32_t>(std::strtoul(body.substr(i + 1, 4).c_str(), nullptr, 16));
                    i += 4;
                    if (cp >= 0xD800 && cp <= 0xDBFF && i + 6 < body.size() &&
                        body[i + 1] == '\\' && body[i + 2] == 'u') {
                        const uint32_t lo = static_cast<uint32_t>(std::strtoul(body.substr(i + 3, 4).c_str(), nullptr, 16));
                        if (lo >= 0xDC00 && lo <= 0xDFFF) {
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                            i += 6;
                        }
                    }
                    append_utf8(value, cp);
                    break;
                }
                default: value += e;
            }
            ++i;
            continue;
        }
        if (c == '"') { *out = value; return i + 1; }
        value += c;
        ++i;
    }
    return std::string::npos;
}

/* Minimal extraction of a string field by key (first occurrence). */
bool json_string(const std::string& body, const char* key, std::string* out) {
    const std::string needle = std::string("\"") + key + "\"";
    size_t pos = body.find(needle);
    if (pos == std::string::npos) return false;
    pos = body.find(':', pos + needle.size());
    if (pos == std::string::npos) return false;
    pos = body.find('"', pos);
    if (pos == std::string::npos) return false;
    return parse_json_string(body, pos, out) != std::string::npos;
}

bool json_number(const std::string& body, const char* key, double* out) {
    const std::string needle = std::string("\"") + key + "\"";
    size_t pos = body.find(needle);
    if (pos == std::string::npos) return false;
    pos = body.find(':', pos + needle.size());
    if (pos == std::string::npos) return false;
    *out = std::atof(body.c_str() + pos + 1);
    return true;
}

/* "messages": [{"role": "...", "content": "..."}, ...] */
bool json_messages(const std::string& body, std::vector<std::string>* roles,
                   std::vector<std::string>* contents) {
    size_t pos = body.find("\"messages\"");
    if (pos == std::string::npos) return false;
    pos = body.find('[', pos);
    if (pos == std::string::npos) return false;
    size_t i = pos + 1;
    while (i < body.size()) {
        const size_t open = body.find('{', i);
        const size_t close_arr = body.find(']', i);
        if (open == std::string::npos || (close_arr != std::string::npos && close_arr < open)) break;
        // scan the object for "role" and "content"
        std::string role = "user", content;
        size_t j = open + 1;
        int depth = 1;
        while (j < body.size() && depth > 0) {
            const char c = body[j];
            if (c == '"') {
                std::string key;
                const size_t after = parse_json_string(body, j, &key);
                if (after == std::string::npos) return false;
                j = after;
                size_t colon = body.find_first_not_of(" \t\r\n", j);
                if (colon != std::string::npos && body[colon] == ':') {
                    size_t v = body.find_first_not_of(" \t\r\n", colon + 1);
                    if (v != std::string::npos && body[v] == '"') {
                        std::string val;
                        const size_t after_v = parse_json_string(body, v, &val);
                        if (after_v == std::string::npos) return false;
                        if (key == "role") role = val;
                        else if (key == "content") content = val;
                        j = after_v;
                        continue;
                    }
                }
                continue;
            }
            if (c == '{') ++depth;
            else if (c == '}') --depth;
            ++j;
        }
        roles->push_back(role);
        contents->push_back(content);
        i = j;
    }
    return !roles->empty();
}

struct Request {
    std::string method;
    std::string path;
    std::string body;
    std::string host;
    std::string auth;
    bool too_large = false;
};

std::string header_value(const std::string& head, const char* name) {
    std::string lower = head;
    for (auto& c : lower) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    std::string needle = std::string("\n") + name + ":";
    for (auto& c : needle) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    const size_t p = lower.find(needle);
    if (p == std::string::npos) return "";
    size_t b = p + needle.size();
    size_t e = head.find("\r\n", b);
    if (e == std::string::npos) e = head.size();
    std::string v = head.substr(b, e - b);
    const size_t f = v.find_first_not_of(" \t");
    const size_t l = v.find_last_not_of(" \t");
    if (f == std::string::npos) return "";
    return v.substr(f, l - f + 1);
}

bool read_request(sock_t s, Request* req) {
    std::string raw;
    char buf[8192];
    size_t header_end = std::string::npos;
    while (raw.size() < 64 * 1024) {
#ifdef _WIN32
        const int n = ::recv(s, buf, sizeof(buf), 0);
#else
        const ssize_t n = ::recv(s, buf, sizeof(buf), 0);
#endif
        if (n <= 0) return false;
        raw.append(buf, static_cast<size_t>(n));
        header_end = raw.find("\r\n\r\n");
        if (header_end != std::string::npos) break;
    }
    if (header_end == std::string::npos) return false;

    const std::string head = raw.substr(0, header_end);
    const size_t sp1 = head.find(' ');
    const size_t sp2 = head.find(' ', sp1 + 1);
    if (sp1 == std::string::npos || sp2 == std::string::npos) return false;
    req->method = head.substr(0, sp1);
    req->path = head.substr(sp1 + 1, sp2 - sp1 - 1);
    const size_t q = req->path.find('?');
    if (q != std::string::npos) req->path.resize(q);
    req->host = header_value(head, "host");
    req->auth = header_value(head, "authorization");

    size_t content_length = 0;
    {
        const std::string cl = header_value(head, "content-length");
        if (!cl.empty()) content_length = std::strtoul(cl.c_str(), nullptr, 10);
    }
    if (content_length > kMaxBody) { req->too_large = true; return true; }
    req->body = raw.substr(header_end + 4);
    while (req->body.size() < content_length) {
#ifdef _WIN32
        const int n = ::recv(s, buf, sizeof(buf), 0);
#else
        const ssize_t n = ::recv(s, buf, sizeof(buf), 0);
#endif
        if (n <= 0) break;
        req->body.append(buf, static_cast<size_t>(n));
    }
    if (req->body.size() > content_length) req->body.resize(content_length);
    return true;
}

void reply(sock_t s, int code, const char* type, const std::string& body) {
    const char* reason = code == 200 ? "OK" : code == 401 ? "Unauthorized" : code == 403 ? "Forbidden"
                       : code == 404 ? "Not Found" : code == 413 ? "Payload Too Large" : "Bad Request";
    std::string head = "HTTP/1.1 " + std::to_string(code) + " " + reason + "\r\n";
    head += std::string("Content-Type: ") + type + "\r\n";
    head += "Content-Length: " + std::to_string(body.size()) + "\r\n";
    head += "Cache-Control: no-store\r\nConnection: close\r\n\r\n";
    send_str(s, head + body);
}

bool loopback_host(const std::string& host) {
    std::string name = host;
    if (!name.empty() && name[0] == '[') {
        const size_t e = name.find(']');
        name = e == std::string::npos ? name : name.substr(1, e - 1);
    } else {
        const size_t c = name.rfind(':');
        if (c != std::string::npos) name.resize(c);
    }
    return name == "127.0.0.1" || name == "localhost" || name == "::1";
}

struct StreamCtx {
    sock_t sock;
    fish_ctx* engine;
    std::atomic<bool> ok{true};
};

void on_token(const char* text, int /*token_id*/, void* user) {
    auto* ctx = static_cast<StreamCtx*>(user);
    if (!ctx || !text || !ctx->ok) return;
    const std::string frame = "data: {\"token\":\"" + json_escape(text) + "\"}\n\n";
    if (!send_str(ctx->sock, frame)) {
        // The client went away: stop burning CPU on an answer nobody reads.
        ctx->ok = false;
        fish_cancel(ctx->engine);
    }
}

} // namespace

int main(int argc, char** argv) {
    const char* model = argc > 1 ? argv[1] : std::getenv("FISH_MODEL");
    const int port = argc > 2 ? std::atoi(argv[2]) : 8731;
    if (const char* t = std::getenv("FISH_TOKEN")) g_token = t;
    if (!model) {
        std::fprintf(stderr, "usage: fish_server <model.gguf> [port]\n"
                             "  FISH_TOKEN=<secret> requires a bearer token on every request\n");
        return 2;
    }

#ifdef _WIN32
    WSADATA wsa{};
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        std::fprintf(stderr, "winsock init failed\n");
        return 1;
    }
#else
    signal(SIGPIPE, SIG_IGN);
#endif

    fish_ctx* ctx = fish_create();
    if (!ctx) return 1;
    if (fish_load(ctx, model, 0) != 0) {
        std::fprintf(stderr, "load failed: %s\n", fish_last_error(ctx));
        return 1;
    }

    sock_t listener = ::socket(AF_INET, SOCK_STREAM, 0);
    if (listener == BAD_SOCK) return 1;
    int yes = 1;
    ::setsockopt(listener, SOL_SOCKET, SO_REUSEADDR,
                 reinterpret_cast<const char*>(&yes), sizeof(yes));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<unsigned short>(port));
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (::bind(listener, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        std::fprintf(stderr, "bind failed on 127.0.0.1:%d\n", port);
        return 1;
    }
    ::listen(listener, 8);
    std::fprintf(stderr, "FISH serving %s on http://127.0.0.1:%d (kernel %s%s)\n",
                 model, port, fish_kernel_name(), g_token.empty() ? "" : ", bearer auth on");

    for (;;) {
        sock_t client = ::accept(listener, nullptr, nullptr);
        if (client == BAD_SOCK) continue;

        Request req;
        if (!read_request(client, &req)) {
            CLOSE_SOCK(client);
            continue;
        }
        if (req.too_large) {
            reply(client, 413, "application/json", "{\"error\":\"body too large\"}");
            CLOSE_SOCK(client);
            continue;
        }
        if (!loopback_host(req.host)) {
            reply(client, 403, "application/json", "{\"error\":\"forbidden host\"}");
            CLOSE_SOCK(client);
            continue;
        }
        if (!g_token.empty() && req.path != "/fish/health") {
            const std::string want = "Bearer " + g_token;
            if (req.auth.size() != want.size() ||
                !std::equal(req.auth.begin(), req.auth.end(), want.begin())) {
                reply(client, 401, "application/json", "{\"error\":\"unauthorized\"}");
                CLOSE_SOCK(client);
                continue;
            }
        }

        if (req.method == "GET" && req.path == "/fish/health") {
            reply(client, 200, "application/json", "{\"status\":\"ok\"}");
        } else if (req.method == "GET" && req.path == "/fish/specs") {
            const char* specs = fish_specs_json(ctx);
            reply(client, 200, "application/json", specs ? specs : "{}");
        } else if (req.method == "GET" && req.path == "/stats") {
            fish_stats st{};
            fish_get_stats(ctx, &st);
            char buf[640];
            std::snprintf(buf, sizeof(buf),
                          "{\"tokens_per_second\":%.2f,\"generated_tokens\":%d,"
                          "\"prompt_tokens\":%d,\"cached_tokens\":%d,\"truncated\":%d,"
                          "\"context_used\":%d,\"context_size\":%d,"
                          "\"prefill_seconds\":%.3f,\"decode_seconds\":%.3f,"
                          "\"prefill_tokens_per_second\":%.1f,\"threads\":%d,\"streaming\":%d}",
                          st.tokens_per_second, st.generated_tokens, st.prompt_tokens,
                          st.cached_tokens, st.truncated, st.context_used, st.context_size,
                          st.prefill_seconds, st.decode_seconds, st.prefill_tokens_per_second,
                          st.n_threads, st.streaming);
            reply(client, 200, "application/json", buf);
        } else if (req.method == "POST" && req.path == "/fish/stream") {
            std::vector<std::string> roles, contents;
            std::string prompt;
            const bool have_messages = json_messages(req.body, &roles, &contents);
            if (!have_messages && !json_string(req.body, "prompt", &prompt)) {
                reply(client, 400, "application/json", "{\"error\":\"missing prompt or messages\"}");
                CLOSE_SOCK(client);
                continue;
            }
            double temperature = 0.7, top_p = 0.9, repeat = 1.1;
            double max_tokens = 512, top_k = 40;
            json_number(req.body, "temperature", &temperature);
            json_number(req.body, "top_p", &top_p);
            json_number(req.body, "max_tokens", &max_tokens);
            json_number(req.body, "top_k", &top_k);
            json_number(req.body, "repeat_penalty", &repeat);

            send_str(client,
                     "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     "Cache-Control: no-store\r\nConnection: close\r\n\r\n");

            StreamCtx sc{client, ctx};
            if (have_messages) {
                std::vector<const char*> r, c;
                for (size_t i = 0; i < roles.size(); ++i) { r.push_back(roles[i].c_str()); c.push_back(contents[i].c_str()); }
                fish_generate_chat(ctx, r.data(), c.data(), static_cast<int>(r.size()),
                                   static_cast<int>(max_tokens), static_cast<float>(temperature),
                                   static_cast<int>(top_k), static_cast<float>(top_p),
                                   static_cast<float>(repeat), on_token, &sc);
            } else {
                fish_generate(ctx, prompt.c_str(), 1, static_cast<int>(max_tokens),
                              static_cast<float>(temperature), static_cast<int>(top_k),
                              static_cast<float>(top_p), static_cast<float>(repeat),
                              on_token, &sc);
            }
            if (sc.ok) send_str(client, "data: [DONE]\n\n");
        } else {
            reply(client, 404, "application/json", "{\"error\":\"not found\"}");
        }
        CLOSE_SOCK(client);
    }
}
