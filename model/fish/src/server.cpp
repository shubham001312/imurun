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
 */
#include <fish/c_api.h>

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>

#ifdef _WIN32
#  include <winsock2.h>
#  include <ws2tcpip.h>
using sock_t = SOCKET;
#  define CLOSE_SOCK closesocket
#  define BAD_SOCK INVALID_SOCKET
#else
#  include <arpa/inet.h>
#  include <netinet/in.h>
#  include <sys/socket.h>
#  include <unistd.h>
using sock_t = int;
#  define CLOSE_SOCK ::close
#  define BAD_SOCK (-1)
#endif

namespace {

constexpr int kMaxBody = 1 << 20;

bool send_all(sock_t s, const char* data, size_t len) {
    while (len > 0) {
#ifdef _WIN32
        const int n = ::send(s, data, static_cast<int>(len), 0);
#else
        const ssize_t n = ::send(s, data, len, 0);
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

/* Minimal extraction of a top-level string field. The body is produced by the
 * Python client, so it is JSON with no exotic escapes in the keys. */
bool json_string(const std::string& body, const char* key, std::string* out) {
    const std::string needle = std::string("\"") + key + "\"";
    size_t pos = body.find(needle);
    if (pos == std::string::npos) return false;
    pos = body.find(':', pos + needle.size());
    if (pos == std::string::npos) return false;
    pos = body.find('"', pos);
    if (pos == std::string::npos) return false;
    std::string value;
    for (size_t i = pos + 1; i < body.size(); ++i) {
        const char c = body[i];
        if (c == '\\' && i + 1 < body.size()) {
            const char e = body[++i];
            switch (e) {
                case 'n': value += '\n'; break;
                case 'r': value += '\r'; break;
                case 't': value += '\t'; break;
                case '"': value += '"'; break;
                case '\\': value += '\\'; break;
                case '/': value += '/'; break;
                default: value += e;
            }
            continue;
        }
        if (c == '"') { *out = value; return true; }
        value += c;
    }
    return false;
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

struct Request {
    std::string method;
    std::string path;
    std::string body;
};

bool read_request(sock_t s, Request* req) {
    std::string raw;
    char buf[8192];
    size_t header_end = std::string::npos;
    while (raw.size() < kMaxBody) {
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

    size_t content_length = 0;
    {
        std::string lower = head;
        for (auto& c : lower) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        const size_t p = lower.find("content-length:");
        if (p != std::string::npos) content_length = std::strtoul(head.c_str() + p + 15, nullptr, 10);
    }
    req->body = raw.substr(header_end + 4);
    while (req->body.size() < content_length && req->body.size() < static_cast<size_t>(kMaxBody)) {
#ifdef _WIN32
        const int n = ::recv(s, buf, sizeof(buf), 0);
#else
        const ssize_t n = ::recv(s, buf, sizeof(buf), 0);
#endif
        if (n <= 0) break;
        req->body.append(buf, static_cast<size_t>(n));
    }
    if (req->body.size() > static_cast<size_t>(content_length)) req->body.resize(content_length);
    return true;
}

void reply(sock_t s, int code, const char* type, const std::string& body) {
    const char* reason = code == 200 ? "OK" : code == 404 ? "Not Found" : "Bad Request";
    std::string head = "HTTP/1.1 " + std::to_string(code) + " " + reason + "\r\n";
    head += std::string("Content-Type: ") + type + "\r\n";
    head += "Content-Length: " + std::to_string(body.size()) + "\r\n";
    head += "Cache-Control: no-store\r\nConnection: close\r\n\r\n";
    send_str(s, head + body);
}

struct StreamCtx {
    sock_t sock;
    std::atomic<bool> ok{true};
};

void on_token(const char* text, int /*token_id*/, void* user) {
    auto* ctx = static_cast<StreamCtx*>(user);
    if (!ctx || !text) return;
    const std::string frame = "data: {\"token\":\"" + json_escape(text) + "\"}\n\n";
    if (!send_str(ctx->sock, frame)) ctx->ok = false;
}

} // namespace

int main(int argc, char** argv) {
    const char* model = argc > 1 ? argv[1] : std::getenv("FISH_MODEL");
    const int port = argc > 2 ? std::atoi(argv[2]) : 8731;
    if (!model) {
        std::fprintf(stderr, "usage: fish_server <model.gguf> [port]\n");
        return 2;
    }

#ifdef _WIN32
    WSADATA wsa{};
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        std::fprintf(stderr, "winsock init failed\n");
        return 1;
    }
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
    std::fprintf(stderr, "FISH serving %s on http://127.0.0.1:%d (kernel %s)\n",
                 model, port, fish_kernel_name());

    for (;;) {
        sock_t client = ::accept(listener, nullptr, nullptr);
        if (client == BAD_SOCK) continue;

        Request req;
        if (!read_request(client, &req)) {
            CLOSE_SOCK(client);
            continue;
        }

        if (req.method == "GET" && req.path == "/fish/health") {
            reply(client, 200, "application/json", "{\"status\":\"ok\"}");
        } else if (req.method == "GET" && req.path == "/fish/specs") {
            const char* specs = fish_specs_json(ctx);
            reply(client, 200, "application/json", specs ? specs : "{}");
        } else if (req.method == "GET" && req.path == "/stats") {
            fish_stats st{};
            fish_get_stats(ctx, &st);
            char buf[512];
            std::snprintf(buf, sizeof(buf),
                          "{\"tokens_per_second\":%.2f,\"generated_tokens\":%d,"
                          "\"context_used\":%d,\"context_size\":%d,"
                          "\"prefill_seconds\":%.3f,\"decode_seconds\":%.3f}",
                          st.tokens_per_second, st.generated_tokens, st.context_used,
                          st.context_size, st.prefill_seconds, st.decode_seconds);
            reply(client, 200, "application/json", buf);
        } else if (req.method == "POST" && req.path == "/fish/stream") {
            std::string prompt;
            if (!json_string(req.body, "prompt", &prompt)) {
                reply(client, 400, "application/json", "{\"error\":\"missing prompt\"}");
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

            StreamCtx sc{client};
            fish_generate(ctx, prompt.c_str(), 1, static_cast<int>(max_tokens),
                          static_cast<float>(temperature), static_cast<int>(top_k),
                          static_cast<float>(top_p), static_cast<float>(repeat),
                          on_token, &sc);
            send_str(client, "data: [DONE]\n\n");
        } else {
            reply(client, 404, "application/json", "{\"error\":\"not found\"}");
        }
        CLOSE_SOCK(client);
    }
}
