// P0-M: read-only ordinary-question markers. Never controls a terminal/process.
#pragma once
#include "attention_source.h"
#include <QCryptographicHash>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QFileSystemWatcher>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSet>
#include <QTimer>
#include <dirent.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <utility>
#include <cerrno>
#include <cstdint>

namespace g2cattention {
namespace marker {

inline bool nonce(const QString &value) {
    if (value.size() != 32) { return false; }
    for (QChar c : value) {
        if (!((c >= QLatin1Char('0') && c <= QLatin1Char('9')) || (c >= QLatin1Char('a') && c <= QLatin1Char('f')))) { return false; }
    }
    return true;
}
inline quint64 number(const QString &text) {
    if (text.isEmpty() || text.size() > 20 || text.startsWith(QLatin1Char('0'))) { return 0; }
    for (QChar c : text) { if (c < QLatin1Char('0') || c > QLatin1Char('9')) { return 0; } }
    bool ok = false; const auto value = text.toULongLong(&ok); return ok ? value : 0;
}
inline bool keys(const QJsonObject &object, const QStringList &names) {
    if (object.size() != names.size()) { return false; }
    for (const auto &name : names) { if (!object.contains(name)) { return false; } }
    return true;
}

class Directory final {
public:
    explicit Directory(int fd = -1) : fd_(fd) {}
    ~Directory() { if (fd_ >= 0) { ::close(fd_); } }
    Directory(const Directory &) = delete;
    Directory &operator=(const Directory &) = delete;
    Directory(Directory &&other) noexcept : fd_(std::exchange(other.fd_, -1)) {}
    Directory &operator=(Directory &&other) noexcept {
        if (fd_ >= 0) { ::close(fd_); } fd_ = std::exchange(other.fd_, -1); return *this;
    }
    int fd() const { return fd_; }
    bool privateDir() const {
        struct stat st{};
        return fd_ >= 0 && ::fstat(fd_, &st) == 0 && S_ISDIR(st.st_mode) && st.st_uid == ::geteuid() && !(st.st_mode & 0077);
    }
    Directory child(const QByteArray &name) const {
        if (fd_ < 0) { return Directory(); }
        return Directory(::openat(fd_, name.constData(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
    }
    static Directory path(const QString &path) {
        if (!QDir::isAbsolutePath(path) || QDir::cleanPath(path) != path || path.contains(QChar::Null)) { return Directory(); }
        Directory dir(::open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC));
        for (const auto &part : path.split(QLatin1Char('/'), Qt::SkipEmptyParts)) { dir = dir.child(QFile::encodeName(part)); }
        return dir;
    }
    // Missing is distinct from malformed: only a verified absence clears yellow.
    bool read(const char *name, QJsonObject &object, bool &missing) const {
        missing = false; object = {};
        const int fd = fd_ < 0 ? -1 : ::openat(fd_, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
        if (fd < 0) { missing = fd_ >= 0 && errno == ENOENT; return missing; }
        struct stat before{}, after{};
        const bool safe = ::fstat(fd, &before) == 0 && S_ISREG(before.st_mode) && before.st_uid == ::geteuid()
            && !(before.st_mode & 0077) && before.st_nlink == 1 && before.st_size > 0 && before.st_size <= 4096;
        QByteArray data;
        if (safe) {
            char bytes[4097];
            while (data.size() <= 4096) {
                const auto count = ::read(fd, bytes, sizeof bytes);
                if (count < 0 && errno == EINTR) { continue; }
                if (count < 0) { data.clear(); break; }
                if (!count) { break; }
                data.append(bytes, int(count));
            }
        }
        const bool stable = ::fstat(fd, &after) == 0 && before.st_size == after.st_size && data.size() == before.st_size
            && before.st_mtim.tv_sec == after.st_mtim.tv_sec && before.st_mtim.tv_nsec == after.st_mtim.tv_nsec;
        ::close(fd);
        if (!safe || !stable) { return false; }
        QJsonParseError error;
        const auto json = QJsonDocument::fromJson(data, &error);
        if (error.error != QJsonParseError::NoError || !json.isObject() || json.toJson(QJsonDocument::Compact) != data) { return false; }
        object = json.object(); return true;
    }
    QByteArray provenance() const {
        struct stat st{};
        if (fd_ < 0 || ::fstat(fd_, &st) != 0) { return {}; }
        return QByteArray::number(st.st_dev) + ':' + QByteArray::number(st.st_ino);
    }
private:
    int fd_;
};

struct Process { quint32 parent = 0; quint64 ticks = 0; };
inline std::optional<Process> process(quint32 pid) {
    struct stat st{};
    const QByteArray path = "/proc/" + QByteArray::number(pid);
    if (!pid || ::stat(path.constData(), &st) != 0 || st.st_uid != ::geteuid()) { return std::nullopt; }
    QFile file(QString::fromLatin1(path) + QStringLiteral("/stat"));
    if (!file.open(QIODevice::ReadOnly)) { return std::nullopt; }
    const QByteArray text = file.read(4097);
    const int end = text.lastIndexOf(')');
    if (end < 0 || text.size() > 4096) { return std::nullopt; }
    const auto fields = text.mid(end + 1).simplified().split(' ');
    if (fields.size() <= 19 || fields[0] == "Z" || fields[0] == "X" || fields[0] == "x") { return std::nullopt; }
    const auto parent = number(QString::fromLatin1(fields[1])), ticks = number(QString::fromLatin1(fields[19]));
    if (parent > UINT32_MAX || !ticks) { return std::nullopt; }
    return Process{quint32(parent), ticks};
}
inline quint64 pidNamespace(quint32 pid) {
    struct stat st{};
    const auto path = "/proc/" + QByteArray::number(pid) + "/ns/pid";
    return ::stat(path.constData(), &st) == 0 ? st.st_ino : 0;
}
inline bool descendant(quint32 pid, quint32 server, quint64 serverTicks) {
    for (int i = 0; i < 128; ++i) {
        const auto p = process(pid); if (!p) { return false; }
        if (pid == server) { return p->ticks == serverTicks; }
        if (p->parent <= 1 || p->parent == pid) { return false; }
        pid = p->parent;
    }
    return false;
}

inline bool binding(const QJsonObject &b, const QString &nonceName, Identity &identity) {
    if (!keys(b, {"version", "project_id", "multiplexer", "session_id", "server_pid", "server_start_ticks",
            "run_nonce", "codex_pid", "codex_start_ticks", "boot_id", "pid_namespace"}) || b.value("version") != 1
        || b.value("run_nonce").toString() != nonceName || !nonce(nonceName)) { return false; }
    identity = {b.value("project_id").toString(), b.value("multiplexer").toString(), b.value("session_id").toString(), b.value("server_start_ticks").toString()};
    const auto server = number(b.value("server_pid").toString()), codex = number(b.value("codex_pid").toString());
    const auto ticks = number(b.value("codex_start_ticks").toString()), space = number(b.value("pid_namespace").toString());
    if (!identity.valid() || !server || !codex || server == codex || server > UINT32_MAX || codex > UINT32_MAX || !ticks || !space) { return false; }
    if (identity.multiplexer == "screen") {
        if (identity.sessionId != QString::number(server) + ".g2c-" + identity.project) { return false; }
    } else {
        const auto parts = identity.sessionId.split('.');
        if (parts.size() != 3 || parts[0] != QString::number(server) || !parts[1].startsWith('$') || !number(parts[2])) { return false; }
        const auto id = parts[1].mid(1); if (id.isEmpty()) { return false; }
        for (QChar c : id) { if (c < QLatin1Char('0') || c > QLatin1Char('9')) { return false; } }
    }
    QFile boot(QStringLiteral("/proc/sys/kernel/random/boot_id"));
    if (!boot.open(QIODevice::ReadOnly) || QString::fromLatin1(boot.read(64)).trimmed() != b.value("boot_id").toString()) { return false; }
    const auto code = process(quint32(codex)), host = process(quint32(server));
    return code && host && code->ticks == ticks && host->ticks == number(identity.generation)
        && pidNamespace(quint32(codex)) == space && pidNamespace(quint32(server)) == space
        && descendant(quint32(codex), quint32(server), host->ticks);
}

} // namespace marker

class MarkerAttentionSource final : public QObject, public AttentionSource {
public:
    explicit MarkerAttentionSource(QString runtime = qEnvironmentVariable("XDG_RUNTIME_DIR")) : runtime_(std::move(runtime)) {
        connect(&watcher_, &QFileSystemWatcher::directoryChanged, this, [this] { reload(); });
        connect(&timer_, &QTimer::timeout, this, [this] { reload(); });
        timer_.setInterval(3000); timer_.start(); reload();
    }
    std::optional<Run> binding(const Identity &id) const override {
        const auto it = candidates_.constFind(id.slot());
        return it != candidates_.cend() && it->identity == id ? std::optional<Run>(it->run) : std::nullopt;
    }
    void watch(const Ticket &ticket, Delivery delivery) override {
        watches_.insert(ticket.viewEpoch, {ticket, std::move(delivery), 0, {}}); deliver(ticket.viewEpoch);
    }
    void unwatch(const Ticket &ticket) override { watches_.remove(ticket.viewEpoch); }

    void reload() {
        using marker::Directory;
        const bool expired = checked_.isValid() && checked_.elapsed() >= 10000;
        checked_.restart();
        bool lostWatch = false;
        const auto watched = watcher_.directories();
        for (const auto &path : wanted_) { if (!watched.contains(path)) { lostWatch = true; } }
        QHash<QString, Candidate> next;
        QSet<QString> ambiguous;
        QStringList desired;
        auto runtime = Directory::path(runtime_);
        if (runtime.privateDir()) {
            desired.append(runtime_);
            auto app = runtime.child("gpt2codex");
            if (app.privateDir()) {
                desired.append(runtime_ + "/gpt2codex");
                auto root = app.child("attention");
                if (root.privateDir()) {
                    const auto path = runtime_ + "/gpt2codex/attention"; desired.append(path);
                    DIR *entries = ::fdopendir(::openat(root.fd(), ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC));
                    int count = 0;
                    if (entries) {
                        while (auto *item = ::readdir(entries)) {
                            const QString name = QString::fromLatin1(item->d_name);
                            if (name == "." || name == "..") { continue; }
                            if (++count > 256) { next.clear(); break; }
                            if (!marker::nonce(name)) { continue; }
                            auto dir = root.child(name.toLatin1());
                            if (!dir.privateDir()) { continue; }
                            desired.append(path + '/' + name);
                            QJsonObject b; bool absent = false;
                            Candidate c;
                            if (!dir.read("binding.json", b, absent) || absent || !marker::binding(b, name, c.identity)) { continue; }
                            QJsonObject wait;
                            if (!dir.read("waiting.json", wait, absent)) { continue; }
                            if (!absent && (!marker::keys(wait, {"version", "binding", "wait_nonce", "status"})
                                || wait.value("version") != 1 || wait.value("binding").toObject() != b
                                || wait.value("status") != "waiting" || !marker::nonce(wait.value("wait_nonce").toString()))) { continue; }
                            c.wait = absent ? QString() : wait.value("wait_nonce").toString();
                            c.provenance = root.provenance() + '/' + dir.provenance()
                                + '/' + QCryptographicHash::hash(QJsonDocument(b).toJson(QJsonDocument::Compact), QCryptographicHash::Sha256).toHex();
                            const auto previous = candidates_.constFind(c.identity.slot());
                            c.run = {name, previous != candidates_.cend() && previous->provenance == c.provenance && !expired && !lostWatch
                                ? previous->run.sourceEpoch : QUuid::createUuid().toString(QUuid::WithoutBraces), {}, true};
                            if (next.contains(c.identity.slot())) { ambiguous.insert(c.identity.slot()); }
                            next.insert(c.identity.slot(), c);
                        }
                        ::closedir(entries);
                    }
                }
            }
        }
        for (const auto &slot : ambiguous) { next.remove(slot); }
        bool changed = next.size() != candidates_.size();
        for (auto it = next.cbegin(); it != next.cend(); ++it) {
            const auto old = candidates_.constFind(it.key());
            if (old == candidates_.cend() || !(old->identity == it->identity) || !(old->run == it->run)) { changed = true; }
        }
        candidates_ = std::move(next);
        // Parent watches discover a missing/recreated attention directory.
        for (const auto &path : watcher_.directories()) { if (!desired.contains(path)) { watcher_.removePath(path); } }
        for (const auto &path : desired) { if (!watcher_.directories().contains(path)) { watcher_.addPath(path); } }
        wanted_ = desired;
        if (changed) { bindingsChanged(); }
        const auto subscriptions = watches_.keys();
        for (const auto &epoch : subscriptions) { deliver(epoch); }
    }

private:
    struct Candidate { Identity identity; Run run; QString wait; QByteArray provenance; };
    struct Watch { Ticket ticket; Delivery callback; quint64 sequence; QString wait; };
    void deliver(const QString &epoch) {
        auto watch = watches_.find(epoch); if (watch == watches_.end()) { return; }
        const auto candidate = candidates_.constFind(watch->ticket.identity.slot());
        if (candidate == candidates_.cend() || !(candidate->identity == watch->ticket.identity) || !(candidate->run == watch->ticket.run)) { return; }
        Event event; event.ticket = watch->ticket; event.sequence = ++watch->sequence;
        event.kind = watch->sequence == 1 ? Kind::WaitingSnapshot : watch->wait == candidate->wait ? Kind::Heartbeat : Kind::WaitingChanged;
        event.waiting = !candidate->wait.isEmpty(); watch->wait = candidate->wait;
        const auto callback = watch->callback; callback(event);
    }
    QString runtime_;
    QFileSystemWatcher watcher_;
    QTimer timer_;
    QElapsedTimer checked_;
    QStringList wanted_;
    QHash<QString, Candidate> candidates_;
    QHash<QString, Watch> watches_;
};

} // namespace g2cattention
