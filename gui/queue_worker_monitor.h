#pragma once
#include <QDateTime>
#include <QRegularExpression>
#include <QHash>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QPlainTextEdit>
#include <QProcess>
#include <QProcessEnvironment>
#include <QProgressBar>
#include <QPushButton>
#include <QScrollBar>
#include <QStandardPaths>
#include <QTimer>
#include <QVBoxLayout>
#include <QHBoxLayout>
#include <cmath>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>

namespace g2cworker {
inline QString timeText(qint64 seconds) {
    return seconds > 0 ? QDateTime::fromSecsSinceEpoch(seconds).toLocalTime().toString("d.M. HH:mm:ss t") : QStringLiteral("N/A");
}
inline QString safeText(QString value) {
    value.remove(QRegularExpression(QStringLiteral("[\\x00-\\x1f\\x7f]")));
    return value.left(128);
}
struct Limit { bool available = false; int remaining = 0; qint64 reset = 0; };
struct Limits { Limit five, week; QString reason, category, extra; };
inline Limits parseLimits(const QJsonObject &payload) {
    Limits result;
    QJsonObject bucket;
    const auto buckets = payload.value("rateLimitsByLimitId").toObject();
    const auto single = payload.value("rateLimits").toObject();
    if (buckets.value("codex").isObject()) {
        bucket = buckets.value("codex").toObject(); result.category = "codex";
    } else if (!single.isEmpty() && ((buckets.isEmpty() && (single.value("limitId").isNull()
            || single.value("limitId").isUndefined())) || single.value("limitId").toString() == "codex")) {
        bucket = single; result.category = single.value("limitId").toString("legacy");
    } else {
        result.reason = QStringLiteral("Server neposkytl primární kategorii codex");
        if (!buckets.isEmpty()) { result.reason += QStringLiteral("; jiné kategorie: ") + safeText(buckets.keys().join(", ")); }
        else if (!single.isEmpty()) { result.reason += ": " + safeText(single.value("limitId").toString()); }
        return result;
    }
    QStringList otherWindows;
    for (const auto &name : {"primary", "secondary"}) {
        const auto w = bucket.value(name).toObject();
        if (w.isEmpty()) { continue; }
        const auto used = w.value("usedPercent");
        const auto duration = w.value("windowDurationMins");
        if (!used.isDouble() || !std::isfinite(used.toDouble()) || !duration.isDouble()) { continue; }
        Limit limit{true, qRound(qBound(0.0, 100.0 - used.toDouble(), 100.0)), 0};
        if (w.value("resetsAt").isDouble()) { limit.reset = w.value("resetsAt").toInteger(); }
        if (duration.toDouble() == 300) { result.five = limit; }
        else if (duration.toDouble() == 10080) { result.week = limit; }
        else { otherWindows << QString::number(duration.toDouble()) + " min"; }
    }
    result.reason = QStringLiteral("Server neposkytl okno 300 / 10080 min");
    if (!otherWindows.isEmpty()) { result.reason += QStringLiteral("; odlišná okna: ") + otherWindows.join(", "); }
    if (buckets.size() > 1) {
        auto other = buckets.keys(); other.removeAll("codex");
        result.category += QStringLiteral(" (mimo ukazatele: ") + safeText(other.join(", ")) + ")";
    }
    const auto credits = bucket.value("credits").toObject();
    if (!credits.isEmpty()) {
        if (credits.value("unlimited").toBool()) { result.extra = QStringLiteral("Workspace kredity: bez omezení"); }
        else if (credits.value("balance").isString()) { result.extra = QStringLiteral("Workspace kreditní zůstatek: ") + safeText(credits.value("balance").toString()); }
        else if (credits.value("hasCredits").isBool()) { result.extra = credits.value("hasCredits").toBool() ? QStringLiteral("Workspace kredity: dostupné") : QStringLiteral("Workspace kredity: nedostupné"); }
    }
    if (bucket.value("spendControlReached").isBool()) {
        result.extra += (result.extra.isEmpty() ? "" : " · ") + QStringLiteral("Spend control: ")
            + (bucket.value("spendControlReached").toBool() ? QStringLiteral("dosažen") : QStringLiteral("nedosažen"));
    }
    const auto spend = bucket.value("individualLimit").toObject();
    if (spend.value("remainingPercent").isDouble()) {
        result.extra += QStringLiteral(" · Individuální spend control: zbývá %1 % (reset %2)")
            .arg(qBound(0, spend.value("remainingPercent").toInt(), 100)).arg(timeText(spend.value("resetsAt").toInteger()));
    }
    return result;
}
inline QString eventLabel(const QString &name) {
    static const QHash<QString, QString> labels{{"poll", "poll"}, {"approved", "approved"},
        {"claimed", "claimed"}, {"screen_started", "screen started"}, {"turn_started", "turn started"},
        {"turn_completed", "turn completed"}, {"handoff_published", "handoff published"},
        {"failed", "FAILED"}, {"worker_started", "worker started"}, {"poll_failed", "POLL ERROR"}, {"launch_started", "Codex launch started"}, {"blocked", "blocked"}, {"unknown", "UNKNOWN — ruční ověření"}};
    return labels.value(name);
}
inline QString eventLine(const QByteArray &line) {
    if (line.size() > 1024) { return {}; }
    const auto o = QJsonDocument::fromJson(line).object();
    const auto label = eventLabel(o.value("event").toString());
    if (o.value("v").toInt() != 1 || label.isEmpty() || o.value("ts").toInteger() <= 0) { return {}; }
    QString text = timeText(o.value("ts").toInteger()) + "  " + label;
    if (o.value("issue").toInt() > 0) { text += QStringLiteral(" · #%1").arg(o.value("issue").toInt()); }
    const auto backend = o.value("backend").toString();
    if (QStringList{"exec", "screen", "project-screen"}.contains(backend)) { text += " · " + backend; }
    const auto mode = o.value("mode").toString();
    if (mode == "read-only" || mode == "workspace-write") { text += " · " + mode; }
    return text;
}
// Incremental bounded read, with inode and prefix detection for rename/copytruncate.
// Partial records survive a tick; a giant record is discarded, never rendered.
class EventTail {
public:
    QString read(const QString &path) {
        QFile f;
        const int fd = ::open(QFile::encodeName(path).constData(), O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
        if (fd < 0) { unavailable = true; return {}; }
        if (!f.open(fd, QIODevice::ReadOnly, QFileDevice::AutoCloseHandle)) { ::close(fd); unavailable = true; return {}; }
        unavailable = false;
        struct stat st{};
        if (::fstat(f.handle(), &st) != 0 || !S_ISREG(st.st_mode)) { unavailable = true; return {}; }
        const auto first = f.peek(256);
        bool reset = st.st_ino != inode_ || st.st_dev != device_ || st.st_size < offset_
            || (!prefix_.isEmpty() && !first.startsWith(prefix_));
        if (!reset && offset_ >= anchor_.size() && !anchor_.isEmpty()) {
            f.seek(offset_ - anchor_.size());
            reset = f.read(anchor_.size()) != anchor_;
        }
        if (reset) { offset_ = 0; partial_.clear(); dropping_ = false; }
        inode_ = st.st_ino; device_ = st.st_dev; prefix_ = first;
        constexpr qint64 cap = 256 * 1024;
        if (f.size() - offset_ > cap) { offset_ = f.size() - cap; partial_.clear(); dropping_ = true; }
        f.seek(offset_);
        const auto data = f.read(cap);
        offset_ = f.pos();
        f.seek(qMax(qint64(0), offset_ - 128)); anchor_ = f.read(qMin(qint64(128), offset_));
        QStringList lines;
        for (char c : data) {
            if (c == '\n') {
                if (!dropping_) { const auto text = eventLine(partial_); if (!text.isEmpty()) { lines << text; } }
                partial_.clear(); dropping_ = false;
            } else if (!dropping_) {
                partial_ += c;
                if (partial_.size() > 1024) { partial_.clear(); dropping_ = true; }
            }
        }
        return lines.join('\n');
    }
    bool unavailable = false;
private:
    quint64 inode_ = 0, device_ = 0;
    qint64 offset_ = 0;
    QByteArray prefix_, partial_, anchor_;
    bool dropping_ = false;
};
inline QString serviceState(int code, const QByteArray &bytes) {
    if (code != 0) { return QStringLiteral("N/A (systemd user bus není dostupný)"); }
    const QString text = QString::fromUtf8(bytes);
    if (text.contains("LoadState=not-found")) { return QStringLiteral("unknown (unit nenalezena)"); }
    if (text.contains("ActiveState=active\n")) { return QStringLiteral("running"); }
    if (text.contains("ActiveState=inactive\n")) { return QStringLiteral("stopped"); }
    if (text.contains("ActiveState=failed\n")) { return QStringLiteral("error"); }
    return QStringLiteral("unknown");
}

class QueueWorkerPage final : public QWidget {
public:
    // live=false gives tests the complete widget without processes, timers or live files.
    QueueWorkerPage(QString logDir, QWidget *parent = nullptr, bool live = true,
                    QString codex = {}) : QWidget(parent), logDir_(std::move(logDir)), codex_(std::move(codex)) {
        setObjectName("queueWorkerPage");
        auto *layout = new QVBoxLayout(this);
        status_ = new QLabel(this); status_->setObjectName("workerStatus"); status_->setWordWrap(true); status_->setTextFormat(Qt::PlainText);
        layout->addWidget(status_);
        log_ = new QPlainTextEdit(this); log_->setObjectName("workerLog"); log_->setReadOnly(true);
        log_->setMaximumBlockCount(2000); log_->setMinimumHeight(70);
        log_->setPlaceholderText(QStringLiteral("Zatím nejsou provozní události. Služba může běžet se starší verzí workeru."));
        layout->addWidget(log_, 1);
        // Deliberately below the expanding log, inside this page only.
        auto *limits = new QHBoxLayout;
        for (int i = 0; i < 2; ++i) {
            auto *column = new QVBoxLayout;
            column->addWidget(new QLabel(i == 0 ? QStringLiteral("5 h") : QStringLiteral("Týdenní limit"), this));
            bars_[i] = new QProgressBar(this); bars_[i]->setRange(0, 100);
            bars_[i]->setObjectName(i == 0 ? "fiveHourRemaining" : "weeklyRemaining");
            column->addWidget(bars_[i]); limits->addLayout(column, 1);
        }
        layout->addLayout(limits);
        auto *footer = new QHBoxLayout;
        limitInfo_ = new QLabel(this); limitInfo_->setObjectName("rateLimitInfo"); limitInfo_->setWordWrap(true); limitInfo_->setTextFormat(Qt::PlainText);
        footer->addWidget(limitInfo_, 1);
        refresh_ = new QPushButton(QStringLiteral("Obnovit limity"), this); footer->addWidget(refresh_);
        layout->addLayout(footer);
        Limits initial; initial.reason = QStringLiteral("Čeká na první čtení App Serveru");
        setLimits(initial, QStringLiteral("Dosud neaktualizováno")); renderStatus();
        connect(refresh_, &QPushButton::clicked, this, [this] { refreshLimits(); });
        if (live) {
            connect(&tick_, &QTimer::timeout, this, [this] { refreshFiles(); }); tick_.start(2000);
            connect(&serviceTimer_, &QTimer::timeout, this, [this] { refreshService(); }); serviceTimer_.start(15000);
            connect(&rateTimer_, &QTimer::timeout, this, [this] { refreshLimits(); }); rateTimer_.start(120000);
            QTimer::singleShot(0, this, [this] { refreshFiles(); refreshService(); refreshLimits(); });
        }
    }
    ~QueueWorkerPage() override {
        // Only our read-only status/RPC clients, never a service or job process.
        for (auto *p : {service_, rpc_}) { if (p) { p->disconnect(this); p->kill(); p->waitForFinished(1000); } }
    }
    void setLogDirectory(const QString &directory) {
        if (directory == logDir_) { return; }
        logDir_ = directory; tail_ = EventTail{}; log_->clear(); snapshot_ = {};
    }
    void setLimits(const Limits &limits, const QString &stamp) {
        for (int i = 0; i < 2; ++i) {
            const Limit &limit = i == 0 ? limits.five : limits.week;
            auto *bar = bars_[i];
            bar->setEnabled(limit.available);
            bar->setValue(limit.available ? limit.remaining : 0);
            bar->setFormat(limit.available ? QStringLiteral("Zbývá %1 %").arg(limit.remaining) : QStringLiteral("N/A"));
            bar->setToolTip(limit.available ? QStringLiteral("Reset: %1. Zbývající kapacita okna; nejde o počet tokenů.").arg(timeText(limit.reset)) : limits.reason);
            bar->setStyleSheet(limit.available && limit.remaining <= 10
                ? QStringLiteral("QProgressBar { color: #dc2626; font-weight: bold; } QProgressBar::chunk { background: #dc2626; }") : QString());
        }
        limitInfo_->setText(stamp + (limits.category.isEmpty() ? "" : " · " + limits.category)
            + ((!limits.five.available || !limits.week.available) ? " · N/A: " + limits.reason : "")
            + (limits.extra.isEmpty() ? "" : "\n" + limits.extra));
    }
    void refreshFiles() {
        if (logDir_.isEmpty()) {
            snapshot_ = {};
            renderStatus(); // No guessed /usr/share or other fallback log path.
            return;
        }
        const QString lines = tail_.read(logDir_ + "/worker-events.jsonl");
        if (!lines.isEmpty()) {
            auto *scroll = log_->verticalScrollBar(); const int position = scroll->value();
            const bool atBottom = position >= scroll->maximum() - 2;
            const int oldBlocks = log_->document()->blockCount();
            log_->appendPlainText(lines);
            const int removed = qMax(0, oldBlocks + lines.count('\n') + 1 - 2000);
            if (atBottom) { scroll->setValue(scroll->maximum()); }
            else { scroll->setValue(qMax(0, position - removed)); }
        }
        QFile file;
        snapshot_ = {};
        const int fd = ::open(QFile::encodeName(logDir_ + "/worker-monitor.json").constData(), O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
        struct stat st{};
        if (fd >= 0 && (::fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || st.st_size > 65536)) { ::close(fd); }
        else if (fd >= 0 && file.open(fd, QIODevice::ReadOnly, QFileDevice::AutoCloseHandle)) {
            const auto value = QJsonDocument::fromJson(file.read(65537)).object();
            if (value.value("v").toInt() == 1) { snapshot_ = value; }
        } else if (fd >= 0) { ::close(fd); }
        renderStatus();
    }
    void renderStatus() {
        QStringList active;
        const auto jobs = snapshot_.value("active").toObject();
        for (auto it = jobs.begin(); it != jobs.end(); ++it) {
            const auto j = it.value().toObject();
            if (j.value("issue").toInt() <= 0) { continue; }
            active << QStringLiteral("#%1 %2 (%3 / %4)").arg(j.value("issue").toInt())
                .arg(eventLabel(j.value("event").toString()), safeText(j.value("backend").toString("N/A")), safeText(j.value("mode").toString("N/A")));
        }
        const auto updated = snapshot_.value("updated_at").toInteger();
        const bool stale = updated > 0 && QDateTime::currentSecsSinceEpoch() - updated > 180;
        status_->setText(QStringLiteral("gpt2codex-github-queue.service: %1\nHost: %2 · backend: %3 · poslední poll: %4%5\nAktivní Issue / fáze: %6%7%8")
            .arg(serviceText_, safeText(snapshot_.value("host").toString("N/A")), safeText(snapshot_.value("backend").toString("N/A")),
                 timeText(snapshot_.value("last_poll").toInteger()), snapshot_.value("poll_error").toBool() ? QStringLiteral(" · poll error") : QString(),
                 active.isEmpty() ? (snapshot_.isEmpty() ? QStringLiteral("N/A") : QStringLiteral("žádná evidovaná")) : active.join("; "),
                 stale ? QStringLiteral(" · starší evidence, aktuální fáze neověřena") : QString(),
                 tail_.unavailable ? QStringLiteral(" · provozní log N/A") : QString()));
    }
    void refreshService() {
        if (service_) { return; }
        auto *p = new QProcess(this); service_ = p;
        p->setStandardErrorFile(QProcess::nullDevice());
        auto finish = [this, p](int code) {
            if (service_ != p) { return; }
            serviceText_ = serviceState(code, p->readAllStandardOutput());
            service_ = nullptr; p->deleteLater(); renderStatus();
        };
        connect(p, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this, [finish](int code, QProcess::ExitStatus) { finish(code); });
        connect(p, &QProcess::errorOccurred, this, [finish](QProcess::ProcessError e) { if (e == QProcess::FailedToStart) { finish(-1); } });
        QTimer::singleShot(3000, p, [this, p] { if (service_ == p) { p->kill(); } });
        p->start("systemctl", {"--user", "show", "gpt2codex-github-queue.service", "--property=LoadState,ActiveState", "--no-pager"});
    }
    void refreshLimits() {
        if (rpc_) { return; }
        if (codex_.isEmpty()) { codex_ = QStandardPaths::findExecutable("codex"); }
        if (codex_.isEmpty()) { rateFailure(QStringLiteral("Místní codex není dostupný")); return; }
        refresh_->setEnabled(false);
        auto *p = new QProcess(this); rpc_ = p; buffer_.clear(); rpcBytes_ = 0; initialized_ = false; answered_ = false;
        p->setProcessChannelMode(QProcess::SeparateChannels);
        // Discard diagnostics and inherited API credentials. Existing Codex login
        // is used internally by app-server; this widget never reads auth files.
        p->setStandardErrorFile(QProcess::nullDevice());
        auto env = QProcessEnvironment::systemEnvironment();
        for (const auto &key : {"GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY"}) { env.remove(key); }
        p->setProcessEnvironment(env);
        connect(p, &QProcess::started, this, [p] {
            p->write("{\"id\":1,\"method\":\"initialize\",\"params\":{\"clientInfo\":{\"name\":\"gpt2codex_worker_monitor\",\"version\":\"1\"}}}\n");
        });
        connect(p, &QProcess::readyReadStandardOutput, this, [this, p] {
            const auto chunk = p->readAllStandardOutput(); rpcBytes_ += chunk.size();
            if (rpcBytes_ > 1024 * 1024) { rateFailure(QStringLiteral("RPC odpověď překročila limit")); answered_ = true; p->kill(); return; }
            buffer_ += chunk;
            int end;
            while ((end = buffer_.indexOf('\n')) >= 0) {
                const auto object = QJsonDocument::fromJson(buffer_.left(end)).object(); buffer_.remove(0, end + 1);
                const auto id = object.value("id");
                if (id.toInt() == 1 && !initialized_) {
                    if (object.contains("error")) { rateFailure(QStringLiteral("App Server inicializace není dostupná")); answered_ = true; p->kill(); return; }
                    initialized_ = true;
                    p->write("{\"method\":\"initialized\"}\n{\"id\":2,\"method\":\"account/rateLimits/read\",\"params\":null}\n");
                } else if (id.toInt() == 2 && !answered_) {
                    answered_ = true;
                    if (object.contains("error")) {
                        rateFailure(object.value("error").toObject().value("code").toInt() == -32601
                            ? QStringLiteral("Místní CLI nepodporuje account/rateLimits/read")
                            : QStringLiteral("Účet nebo server limity neposkytl (RPC chyba)"));
                    } else { setLimits(parseLimits(object.value("result").toObject()), QStringLiteral("Aktualizace: ") + timeText(QDateTime::currentSecsSinceEpoch())); }
                    p->closeWriteChannel(); p->terminate();
                }
                // Server-initiated requests get no authority or auth material.
                else if (!id.isUndefined() && object.contains("method")) {
                    const QJsonObject denied{{"id", id}, {"error", QJsonObject{{"code", -32601}, {"message", "Read-only client"}}}};
                    p->write(QJsonDocument(denied).toJson(QJsonDocument::Compact) + '\n');
                }
            }
        });
        auto finish = [this, p] {
            if (rpc_ != p) { return; }
            if (!answered_) { rateFailure(QStringLiteral("App Server nedostupný nebo vypršel časový limit")); }
            rpc_ = nullptr; refresh_->setEnabled(true); p->deleteLater();
        };
        connect(p, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this, [finish](int, QProcess::ExitStatus) { finish(); });
        connect(p, &QProcess::errorOccurred, this, [finish](QProcess::ProcessError e) { if (e == QProcess::FailedToStart) { finish(); } });
        QTimer::singleShot(20000, p, [this, p] { if (rpc_ == p) { p->kill(); } });
        p->start(codex_, {"app-server", "--listen", "stdio://"});
    }
private:
    void rateFailure(const QString &reason) { Limits l; l.reason = reason; setLimits(l, QStringLiteral("Pokus: ") + timeText(QDateTime::currentSecsSinceEpoch())); }
    QString logDir_, codex_, serviceText_ = QStringLiteral("N/A (dosud neověřeno)");
    QLabel *status_, *limitInfo_;
    QPlainTextEdit *log_;
    QProgressBar *bars_[2];
    QPushButton *refresh_;
    QTimer tick_, serviceTimer_, rateTimer_;
    EventTail tail_;
    QJsonObject snapshot_;
    QProcess *service_ = nullptr, *rpc_ = nullptr;
    QByteArray buffer_;
    qint64 rpcBytes_ = 0;
    bool initialized_ = false, answered_ = false;
};
} // namespace g2cworker
