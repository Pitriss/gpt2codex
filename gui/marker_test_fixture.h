// Owned synthetic processes/registries only, used by Qt marker regressions.
#pragma once
#include <QDebug>
#include <QJsonArray>
#include <QRegularExpression>
#include <QSaveFile>
#include <QProcessEnvironment>
#include <sys/socket.h>
#include <sys/un.h>
#include <cstring>

namespace g2cmarkertest {

inline bool write(const QString &path, const QByteArray &bytes) {
    QSaveFile file(path);
    return file.open(QIODevice::WriteOnly) && file.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner)
        && file.write(bytes) == bytes.size() && file.commit();
}
inline bool json(const QString &path, const QJsonObject &value) { return write(path, QJsonDocument(value).toJson(QJsonDocument::Compact)); }
inline QByteArray bytes(const QString &path) { QFile file(path); return file.open(QIODevice::ReadOnly) ? file.readAll() : QByteArray(); }
inline bool privateDir(const QString &path) {
    return QDir().mkpath(path) && QFile::setPermissions(path, QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner);
}

class Fixture final {
public:
    struct Actor { std::unique_ptr<QProcess> process; g2cattention::Identity identity; QString nonce, directory; QJsonObject binding; };
    QTemporaryDir temp{qEnvironmentVariable("G2C_MARKER_FIXTURE_BASE", QDir::tempPath()) + QStringLiteral("/mm-XXXXXX")};
    QString base, runtime, state, config, cli, listing;
    QList<std::shared_ptr<Actor>> actors;
    QList<int> sockets;
    QString error;

    bool setup() {
        if (!temp.isValid()) { return false; }
        base = QDir(temp.path()).canonicalPath(); runtime = base + "/r"; state = base + "/s";
        config = base + "/c/config.toml"; cli = QCoreApplication::applicationDirPath() + "/gpt2codex";
        if (!QFileInfo(cli).isExecutable()) { error = "private/staged Rust CLI missing"; return false; }
        for (const auto &path : {runtime, state, state + "/gpt2codex", state + "/gpt2codex/screen-sessions",
                state + "/gpt2codex/screen-sessions/tmux-sockets", base + "/c/projects", base + "/h", base + "/fake"}) {
            if (!privateDir(path)) { return false; }
        }
        if (!write(config, "[workspace]\nproject_dir=" + QJsonDocument(QJsonArray{base + "/repos"}).toJson(QJsonDocument::Compact).mid(1).chopped(1) + '\n')) { return false; }
        const QByteArray program =
            "#!/usr/bin/python3\nimport json,os,subprocess,sys\n"
            "assert sys.argv[1]=='--add-dir'\n"
            "print(json.dumps({'ready':os.environ['G2C_ATTENTION_RUN']}),flush=True)\n"
            "for line in sys.stdin:\n"
            " args=line.strip().split()\n"
            " if not args:continue\n"
            " if args[0] not in ('mark','clear'):break\n"
            " p=subprocess.run([os.environ['G2C_ATTENTION_HELPER'],'attention']+args,capture_output=True,text=True)\n"
            " print(json.dumps({'code':p.returncode,'output':p.stdout.strip(),'error':p.stderr.strip()}),flush=True)\n";
        if (!write(base + "/fake/codex", program) || !QFile::setPermissions(base + "/fake/codex", QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner)) { return false; }
        listing = "multiplexer=screen generation=- project_id=bridge screen_name=g2c-bridge state=not_registered session_id=- task=Bridge\n";
        return true;
    }

    QJsonObject reply(QProcess &process, const QByteArray &command = {}) {
        if (!command.isEmpty()) { process.write(command + '\n'); process.waitForBytesWritten(1000); }
        QElapsedTimer deadline; deadline.start();
        while (!process.canReadLine() && deadline.elapsed() < 15000 && process.state() != QProcess::NotRunning) { process.waitForReadyRead(100); }
        const auto result = QJsonDocument::fromJson(process.readLine()).object();
        if (result.isEmpty()) { error = QString::fromUtf8(process.readAllStandardError()).left(300); }
        return result;
    }

    // Diagnostic only: compare immutable start ticks of the owned synthetic server.
    // The Rust probe is an uninstalled test harness, not a new CLI operation.
    void diagnoseGeneration(const QString &recordPath, quint32 server, quint64 ticks,
                            QProcessEnvironment env) {
        static bool reported = false;
        if (reported) { return; }
        reported = true;
        const auto serialized = bytes(recordPath);
        const QRegularExpression jsonNumber(QStringLiteral("\"process_start_ticks\"\\s*:\\s*([-+0-9.eE]{1,32})\\s*[,}]"));
        const auto match = jsonNumber.match(QString::fromUtf8(serialized));
        const auto jsonToken = match.hasMatch() ? match.captured(1) : QStringLiteral("unavailable");
        const auto qtReadback = QJsonDocument::fromJson(serialized).object()
            .value("process_start_ticks").toInteger(-1);
        QString procToken = QStringLiteral("unavailable");
        QFile statFile(QStringLiteral("/proc/") + QString::number(server) + QStringLiteral("/stat"));
        if (statFile.open(QIODevice::ReadOnly)) {
            const auto raw = statFile.read(4097);
            const auto end = raw.lastIndexOf(')');
            if (end >= 0 && raw.size() <= 4096) {
                const auto fields = raw.mid(end + 1).simplified().split(' ');
                const auto token = QString::fromLatin1(fields.value(19));
                if (QRegularExpression(QStringLiteral("^[0-9]{1,20}$")).match(token).hasMatch()) {
                    procToken = token;
                }
            }
        }
        qWarning().noquote() << QStringLiteral("MARKER_GENERATION_QT proc_raw_ticks=%1 parsed_ticks=%2 json_raw_ticks=%3 json_qt_ticks=%4")
            .arg(procToken, QString::number(ticks), jsonToken, QString::number(qtReadback));
        const QString executable = qEnvironmentVariable("G2C_MARKER_RUST_PROBE");
        if (executable.isEmpty() || !QFileInfo(executable).isExecutable()) {
            qWarning("MARKER_GENERATION_RUST status=probe_unavailable");
            return;
        }
        env.insert("G2C_MARKER_PROBE_RECORD", recordPath);
        QProcess probe;
        probe.setProcessEnvironment(env);
        probe.start(executable, {"--exact", "attention_marker::tests::synthetic_gui_generation_probe",
            "--ignored", "--nocapture", "--test-threads=1"});
        if (!probe.waitForStarted(3000) || !probe.waitForFinished(10000)) {
            if (probe.state() != QProcess::NotRunning) { probe.kill(); probe.waitForFinished(1000); }
            qWarning("MARKER_GENERATION_RUST status=probe_start_or_timeout");
            return;
        }
        const QRegularExpression resultPattern(QStringLiteral("^MARKER_GENERATION_RUST record_ticks=[0-9]{1,20} proc_ticks=([0-9]{1,20}|unavailable)$"));
        bool found = false;
        for (const auto &line : probe.readAllStandardOutput().split('\n')) {
            const auto message = QString::fromLatin1(line);
            if (resultPattern.match(message).hasMatch()) { qWarning().noquote() << message; found = true; }
        }
        if (!found || probe.exitStatus() != QProcess::NormalExit || probe.exitCode() != 0) {
            qWarning("MARKER_GENERATION_RUST status=probe_failed");
        }
    }

    std::shared_ptr<Actor> start(const QString &project, const QString &mux) {
        const QString root = base + "/repos/" + project;
        // Attention tests need a valid registered project, not an empty fake Git checkout.
        // Non-Git is a supported project type and keeps this fixture independent of Git.
        if (!privateDir(root) || !write(base + "/c/projects/" + project + ".toml",
                "[project]\ntype='non-git'\nid='" + project.toUtf8() + "'\ndirectory='" + project.toUtf8() + "'\n")) { return {}; }
        const auto server = quint32(::getpid()); const auto provenance = g2cattention::marker::process(server);
        if (!provenance) { return {}; }
        auto actor = std::make_shared<Actor>();
        actor->identity = {project, mux, mux == "screen" ? QString::number(server) + ".g2c-" + project : QString::number(server) + ".$0.12345", QString::number(provenance->ticks)};
        QJsonObject record{{"version", 1}, {"project_id", project}, {"screen_name", "g2c-" + project}, {"task", "Fixture"},
            {"root", root}, {"full_id", actor->identity.sessionId}, {"pid", int(server)}, {"process_start_ticks", qint64(provenance->ticks)}, {"multiplexer", mux}};
        if (mux == "tmux") {
            const QString path = state + "/gpt2codex/screen-sessions/tmux-sockets/" + project + ".sock";
            sockaddr_un address{}; address.sun_family = AF_UNIX;
            const auto name = QFile::encodeName(path);
            if (name.size() >= int(sizeof address.sun_path)) { error = "fixture socket path too long"; return {}; }
            std::memcpy(address.sun_path, name.constData(), name.size() + 1);
            const int socket = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
            if (socket < 0) { error = "fixture socket failed errno=" + QString::number(errno); return {}; } sockets.append(socket);
            if (::bind(socket, reinterpret_cast<sockaddr *>(&address), sizeof address) != 0 || ::chmod(name.constData(), 0600) != 0) {
                error = "fixture bind failed errno=" + QString::number(errno); return {};
            }
            struct stat st{}; if (::lstat(name.constData(), &st) != 0) { return {}; }
            record.insert("tmux", QJsonObject{{"server_pid", int(server)}, {"session_id", "$0"}, {"created", 12345},
                {"socket_dev", qint64(st.st_dev)}, {"socket_ino", qint64(st.st_ino)}});
        }
        const QString suffix = mux == "tmux" ? ".tmux.json" : ".json";
        const QString recordPath = state + "/gpt2codex/screen-sessions/" + project + suffix;
        if (!json(recordPath, record)) { return {}; }
        actor->process = std::make_unique<QProcess>();
        auto env = QProcessEnvironment::systemEnvironment();
        env.insert("HOME", base + "/h"); env.insert("XDG_RUNTIME_DIR", runtime); env.insert("XDG_STATE_HOME", state);
        env.insert("XDG_CONFIG_HOME", base + "/c"); env.insert("PATH", base + "/fake:" + env.value("PATH"));
        actor->process->setProcessEnvironment(env); actor->process->setWorkingDirectory(root);
        actor->process->start(cli, {"attention", "run", "--id", project, "--multiplexer", mux, "--config", config, "--approve-run"});
        if (!actor->process->waitForStarted(3000)) { return {}; }
        const auto ready = reply(*actor->process);
        actor->nonce = ready.value("ready").toString();
        if (!g2cattention::marker::nonce(actor->nonce)) {
            if (error.contains("reason=server_generation_mismatch")) {
                diagnoseGeneration(recordPath, server, provenance->ticks, env);
            }
            return {};
        }
        actor->directory = runtime + "/gpt2codex/attention/" + actor->nonce;
        actor->binding = QJsonDocument::fromJson(bytes(actor->directory + "/binding.json")).object();
        actors.append(actor);
        listing += "multiplexer=" + mux + " generation=" + actor->identity.generation + " project_id=" + project
            + " screen_name=g2c-" + project + " state=detached session_id=" + actor->identity.sessionId
            + " program=running exit_code=- exit_signal=- exited_at=-"
            + (mux == "screen" ? " window=0 window_title=g2c-" + project : " window=- window_title=-")
            + " task=Fixture\n";
        return actor;
    }
    QString mark(const std::shared_ptr<Actor> &actor) {
        const auto result = reply(*actor->process, "mark");
        const auto output = result.value("output").toString();
        return result.value("code").toInt(-1) == 0 && output.startsWith("ATTENTION_WAIT=") ? output.mid(15) : QString();
    }
    int clear(const std::shared_ptr<Actor> &actor, const QString &wait) {
        return reply(*actor->process, "clear --wait " + wait.toLatin1()).value("code").toInt(-1);
    }
    void stop(const std::shared_ptr<Actor> &actor) {
        if (actor->process->state() == QProcess::NotRunning) { return; }
        actor->process->closeWriteChannel();
        if (!actor->process->waitForFinished(3000)) { actor->process->kill(); actor->process->waitForFinished(1000); }
    }
    ~Fixture() { for (const auto &actor : actors) { stop(actor); } for (int socket : sockets) { ::close(socket); } }
};
} // namespace g2cmarkertest
