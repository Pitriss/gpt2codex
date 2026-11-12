#pragma once
#include <QtTest/QtTest>
#include "marker_test_fixture.h"

namespace {

// No PTYs, screen clients or Codex processes: exercise the actual SessionPage
// and Qt shortcut delivery with a memory-only backend.
class MemoryTerminal final : public QWidget {
    Q_OBJECT
public:
    using QWidget::QWidget;
    Q_INVOKABLE bool pasteClipboardText() { ++pastes; return true; }
    void finish() { Q_EMIT terminalFinished(); }
    int pastes = 0;
protected:
    bool event(QEvent *event) override {
        if (event->type() == QEvent::ShortcutOverride) {
            // Emulate TerminalDisplay claiming keys before window shortcuts.
            event->accept();
            return true;
        }
        return QWidget::event(event);
    }
Q_SIGNALS:
    void terminalFinished();
};

class MemoryBackend final : public Gpt2CodexTerminalBackend {
public:
    QString id() const override { return QStringLiteral("konsole"); }
    bool usable() const override { return true; }
    QWidget *create(QWidget *parent, const QString &, const QStringList &args, const QString &) override {
        arguments = args;
        ++creates;
        last = new MemoryTerminal(parent);
        return last;
    }
    void start(QWidget *) override { ++starts; }
    void refreshAppearance(QWidget *) override {}
    QPointer<MemoryTerminal> last;
    QStringList arguments;
    int creates = 0;
    int starts = 0;
};

class GuiLifecycleTests final : public QObject {
    Q_OBJECT
    Session live = [] {
        Session value{QStringLiteral("bridge"), QStringLiteral("g2c-bridge"),
            QStringLiteral("detached"), QStringLiteral("123.g2c-bridge"), QStringLiteral("Bridge")};
        value.programState = QStringLiteral("running");
        value.windowNumber = QStringLiteral("0");
        value.windowTitle = QStringLiteral("g2c-bridge");
        return value;
    }();
    Session missing{QStringLiteral("bridge"), QStringLiteral("g2c-bridge"),
        QStringLiteral("not_registered"), QStringLiteral("-"), QStringLiteral("Bridge")};

    static QString fixtureTemplate() {
        return QString::fromUtf8(G2C_GUI_TEST_WORK_DIR) + QStringLiteral("/tab-icons-XXXXXX");
    }
    static QRect opaqueBounds(const QImage &image) {
        QRect bounds;
        for (int y = 0; y < image.height(); ++y) {
            for (int x = 0; x < image.width(); ++x) {
                if (image.pixelColor(x, y).alpha() > 0) { bounds |= QRect(x, y, 1, 1); }
            }
        }
        return bounds;
    }
    static g2cattention::Identity attentionId(const QString &project = "alpha", const QString &mux = "screen") {
        return {project, mux, mux == "screen" ? "101.g2c-" + project : QStringLiteral("202.$0.111111"), "11"};
    }
    static g2cattention::Run attentionRun(bool waitingOnly = false) {
        return {"synthetic-run", "synthetic-source", waitingOnly ? QString() : QStringLiteral("synthetic-thread"), waitingOnly};
    }
    static g2cattention::Event attentionEvent(const g2cattention::Ticket &ticket, quint64 sequence = 1,
            g2cattention::Kind kind = g2cattention::Kind::Snapshot, g2cattention::Phase phase = g2cattention::Phase::Running) {
        return {ticket, sequence, kind, "synthetic-turn-1", 1, phase, {}, true};
    }
private Q_SLOTS:
    void workerRateLimitPayloads() {
        using namespace g2cworker;
        const QJsonObject five{{"usedPercent", 99}, {"windowDurationMins", 300}, {"resetsAt", 1730947200}};
        const QJsonObject week{{"usedPercent", 25}, {"windowDurationMins", 10080}, {"resetsAt", 1731950800}};
        QJsonObject bucket{{"limitId", "codex"}, {"secondary", five}, {"primary", week}};
        auto parsed = parseLimits({{"rateLimits", bucket}});
        QVERIFY(parsed.five.available); QCOMPARE(parsed.five.remaining, 1);
        QCOMPARE(parsed.five.reset, qint64(1730947200)); QCOMPARE(parsed.week.remaining, 75);
        QCOMPARE(QDateTime::fromSecsSinceEpoch(parsed.five.reset).toUTC().date(), QDate(2024, 11, 7));
        QJsonObject other{{"limitId", "other"}, {"primary", five}};
        parsed = parseLimits({{"rateLimits", other}, {"rateLimitsByLimitId", QJsonObject{{"other", other}, {"codex", bucket}}}});
        QCOMPARE(parsed.five.remaining, 1); QVERIFY(parsed.category.startsWith("codex"));
        parsed = parseLimits({{"rateLimits", other}, {"rateLimitsByLimitId", QJsonObject{{"other", other}}}});
        QVERIFY(!parsed.five.available); QVERIFY(parsed.reason.contains("other"));
        QVERIFY(!parseLimits({}).five.available);
        QVERIFY(!parseLimits({{"rateLimits", QJsonObject{{"primary", five}}}, {"rateLimitsByLimitId", QJsonObject{{"other", other}}}}).five.available);
        bucket["primary"] = QJsonObject{{"usedPercent", 1}, {"windowDurationMins", 60}};
        bucket["secondary"] = QJsonValue::Null;
        parsed = parseLimits({{"rateLimits", bucket}});
        QVERIFY(!parsed.five.available); QVERIFY(parsed.reason.contains("60 min"));
        bucket["primary"] = QJsonObject{{"usedPercent", "99"}, {"windowDurationMins", 300}};
        QVERIFY(!parseLimits({{"rateLimits", bucket}}).five.available);
        bucket["primary"] = QJsonObject{{"usedPercent", 120}, {"windowDurationMins", 300}};
        QCOMPARE(parseLimits({{"rateLimits", bucket}}).five.remaining, 0);
        bucket["primary"] = QJsonObject{{"usedPercent", -10}, {"windowDurationMins", 300}};
        QCOMPARE(parseLimits({{"rateLimits", bucket}}).five.remaining, 100);
        bucket["credits"] = QJsonObject{{"balance", "12.5"}, {"hasCredits", true}, {"unlimited", false}};
        bucket["spendControlReached"] = true;
        parsed = parseLimits({{"rateLimits", bucket}});
        QVERIFY(parsed.extra.contains("12.5")); QVERIFY(parsed.extra.contains("dosažen"));
    }
    void workerEventsRotationAndTruncation() {
        using namespace g2cworker;
        QTemporaryDir fixture(fixtureTemplate()); QVERIFY(fixture.isValid());
        const auto path = fixture.path() + "/events";
        auto event = [](const char *name) { return QJsonDocument(QJsonObject{{"v", 1}, {"ts", 1730947200}, {"issue", 17}, {"event", name}, {"prompt", "DO NOT DISPLAY"}}).toJson(QJsonDocument::Compact) + '\n'; };
        auto write = [&](const QByteArray &data, QIODevice::OpenMode mode = QIODevice::WriteOnly) {
            QFile f(path); return f.open(mode) && f.write(data) == data.size();
        };
        EventTail tail;
        QVERIFY(tail.read(path).isEmpty()); QVERIFY(tail.unavailable);
        QVERIFY(write({})); QVERIFY(tail.read(path).isEmpty()); QVERIFY(!tail.unavailable);
        auto row = event("claimed"); QVERIFY(write(row.left(10), QIODevice::Append));
        QVERIFY(tail.read(path).isEmpty()); QVERIFY(write(row.mid(10), QIODevice::Append));
        auto text = tail.read(path); QVERIFY(text.contains("claimed")); QVERIFY(text.contains("#17")); QVERIFY(!text.contains("DO NOT DISPLAY"));
        QVERIFY(tail.read(path).isEmpty());
        QVERIFY(QFile::rename(path, path + ".1")); QVERIFY(write(event("turn_started")));
        QVERIFY(tail.read(path).contains("turn started"));
        QVERIFY(write(event("poll"))); QVERIFY(tail.read(path).contains("poll"));
        QVERIFY(write("{broken}\n" + event("failed"))); QVERIFY(tail.read(path).contains("FAILED"));
        QVERIFY(eventLine(event("invented")).isEmpty());
        QVERIFY(QFile::remove(path)); QVERIFY(::mkfifo(QFile::encodeName(path).constData(), 0600) == 0);
        QVERIFY(tail.read(path).isEmpty()); QVERIFY(tail.unavailable); QVERIFY(QFile::remove(path));
        QVERIFY(write(QByteArray(400000, 'x') + '\n' + event("handoff_published")));
        text = tail.read(path); QVERIFY(text.contains("handoff published")); QVERIFY(text.size() < 200);
    }
    void workerReadOnlyLayoutAndStates() {
        using namespace g2cworker;
        QTemporaryDir fixture(fixtureTemplate()); QVERIFY(fixture.isValid());
        QueueWorkerPage page(fixture.path(), nullptr, false); page.resize(700, 360); page.show();
        auto *log = page.findChild<QPlainTextEdit *>("workerLog"); QVERIFY(log && log->isReadOnly());
        auto *five = page.findChild<QProgressBar *>("fiveHourRemaining");
        auto *week = page.findChild<QProgressBar *>("weeklyRemaining"); QVERIFY(five && week);
        QCOMPARE(page.findChildren<QPushButton *>().size(), 1);
        QVERIFY(five->geometry().top() > log->geometry().bottom()); QVERIFY(week->isVisible());
        QVERIFY(week->geometry().bottom() <= page.height());
        QVERIFY(!five->isEnabled()); QCOMPARE(five->format(), QString("N/A"));
        Limits limits; limits.five = {true, 1, 1730947200}; limits.week = {true, 80, 1731950800};
        page.setLimits(limits, "synthetic"); QCOMPARE(five->value(), 1); QCOMPARE(five->format(), QString("Zbývá 1 %"));
        QVERIFY(five->styleSheet().contains("#dc2626")); QVERIFY(five->toolTip().contains(timeText(1730947200)));
        page.refreshFiles(); QVERIFY(page.findChild<QLabel *>("workerStatus")->text().contains("N/A"));
        QFile state(fixture.path() + "/worker-monitor.json"); QVERIFY(state.open(QIODevice::WriteOnly));
        state.write(R"({"v":1,"host":"fixture","backend":"screen","last_poll":1730947200,"poll_error":true,"active":{"17":{"issue":17,"event":"turn_started","backend":"screen","mode":"workspace-write"}}})"); state.close();
        page.refreshFiles(); const auto status = page.findChild<QLabel *>("workerStatus")->text();
        QVERIFY(status.contains("#17 turn started")); QVERIFY(status.contains("poll error"));
        QCOMPARE(serviceState(1, "ActiveState=active\n"), QString("N/A (systemd user bus není dostupný)"));
        QCOMPARE(serviceState(0, "LoadState=loaded\nActiveState=inactive\n"), QString("stopped"));
        QCOMPARE(serviceState(0, "LoadState=loaded\nActiveState=active\n"), QString("running"));
        QCOMPARE(serviceState(0, "ActiveState=failed\n"), QString("error"));
        QCOMPARE(serviceState(0, "garbage"), QString("unknown"));
        QFile events(fixture.path() + "/worker-events.jsonl"); QVERIFY(events.open(QIODevice::WriteOnly));
        for (int i = 0; i < 300; ++i) { events.write("{\"v\":1,\"ts\":1730947200,\"event\":\"poll\"}\n"); } events.close();
        page.refreshFiles(); auto *scroll = log->verticalScrollBar(); QVERIFY(scroll->maximum() > 0); scroll->setValue(20);
        QVERIFY(events.open(QIODevice::Append)); events.write("{\"v\":1,\"ts\":1730947200,\"event\":\"failed\"}\n"); events.close();
        page.refreshFiles(); QCOMPARE(scroll->value(), 20);
        page.setLimits({}, "unavailable"); QVERIFY(!five->isEnabled()); QCOMPARE(five->format(), QString("N/A"));
    }
    void workerRpcMockReadOnly() {
        using namespace g2cworker;
        QTemporaryDir fixture(fixtureTemplate()); QVERIFY(fixture.isValid());
        const QString cli = fixture.path() + "/codex";
        QFile f(cli); QVERIFY(f.open(QIODevice::WriteOnly));
        f.write("#!/usr/bin/python3\nimport json,sys,pathlib\nfor line in sys.stdin:\n o=json.loads(line)\n with open(pathlib.Path(__file__).with_name('calls'),'a') as f:f.write(o['method']+'\\n')\n if o['method']=='initialize':print(json.dumps({'id':1,'result':{}}),flush=True)\n if o['method']=='account/rateLimits/read':print(json.dumps({'id':2,'result':{'rateLimits':{'primary':{'usedPercent':99,'windowDurationMins':300,'resetsAt':1730947200},'secondary':{'usedPercent':40,'windowDurationMins':10080}}}}),flush=True)\n"); f.close();
        QVERIFY(QFile::setPermissions(cli, QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner));
        QueueWorkerPage page(fixture.path(), nullptr, false, cli); page.refreshLimits();
        auto *bar = page.findChild<QProgressBar *>("fiveHourRemaining");
        QTRY_VERIFY(bar->isEnabled()); QCOMPARE(bar->value(), 1);
        QFile calls(fixture.path() + "/calls"); QVERIFY(calls.open(QIODevice::ReadOnly));
        QCOMPARE(calls.readAll(), QByteArray("initialize\ninitialized\naccount/rateLimits/read\n"));
    }

    void workerRpcMockUnavailable_data() {
        QTest::addColumn<QByteArray>("response");
        QTest::addColumn<QString>("reason");
        QTest::newRow("missing-method") << QByteArray(R"({"id":2,"error":{"code":-32601,"message":"SECRET"}})") << QString("nepodporuje");
        QTest::newRow("account-unavailable") << QByteArray(R"({"id":2,"error":{"code":-32000,"message":"SECRET"}})") << QString("RPC chyba");
        QTest::newRow("no-windows") << QByteArray(R"({"id":2,"result":{"rateLimits":{}}})") << QString("kategorii codex");
    }
    void workerRpcMockUnavailable() {
        using namespace g2cworker;
        QFETCH(QByteArray, response); QFETCH(QString, reason);
        QTemporaryDir fixture(fixtureTemplate()); QVERIFY(fixture.isValid());
        const QString cli = fixture.path() + "/codex";
        QFile data(fixture.path() + "/response"); QVERIFY(data.open(QIODevice::WriteOnly)); data.write(response); data.close();
        QFile f(cli); QVERIFY(f.open(QIODevice::WriteOnly));
        f.write("#!/usr/bin/python3\nimport json,sys,pathlib\nfor line in sys.stdin:\n o=json.loads(line)\n if o['method']=='initialize':print('{\"id\":1,\"result\":{}}',flush=True)\n if o['method']=='account/rateLimits/read':print(pathlib.Path(__file__).with_name('response').read_text(),flush=True)\n"); f.close();
        QVERIFY(QFile::setPermissions(cli, QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner));
        QueueWorkerPage page(fixture.path(), nullptr, false, cli);
        page.refreshLimits(); page.refreshLimits(); // in-flight reads are coalesced
        auto *info = page.findChild<QLabel *>("rateLimitInfo");
        QTRY_VERIFY(info->text().contains(reason)); QVERIFY(!info->text().contains("SECRET"));
        auto *bar = page.findChild<QProgressBar *>("fiveHourRemaining");
        QVERIFY(!bar->isEnabled()); QCOMPARE(bar->format(), QString("N/A"));
    }
    void configuredProjectsAndBridgeDetached() {
        QTemporaryDir fixture(fixtureTemplate()); QVERIFY(fixture.isValid());
        const QString base = fixture.path(), cli = base + "/bridge";
        auto write = [](const QString &path, const QByteArray &data) {
            QFile f(path); return f.open(QIODevice::WriteOnly) && f.write(data) == data.size();
        };
        const QByteArray exact = "multiplexer=screen generation=79724035 project_id=bridge screen_name=g2c-bridge state=detached session_id=1919828.g2c-bridge program=running exit_code=- exit_signal=- exited_at=- window=0 window_title=g2c-bridge task=Bridge\n";
        QList<Session> parsed; QString error;
        QVERIFY(parseSessions(exact, parsed, error));
        QCOMPARE(parsed[0].state, QString("detached"));
        QVERIFY(write(base + "/sessions", exact
            + "multiplexer=screen generation=123 project_id=kencoder screen_name=g2c-kencoder state=detached session_id=1131254.g2c-kencoder program=running exit_code=- exit_signal=- exited_at=- window=0 window_title=g2c-kencoder task=KEncoder\n"
            + "multiplexer=screen generation=- project_id=m365-tui screen_name=g2c-m365-tui state=not_registered session_id=- task=-\n"
            + "multiplexer=tmux generation=- project_id=bridge screen_name=g2c-bridge state=not_registered session_id=- task=-\n"
            + "multiplexer=tmux generation=- project_id=m365-tui screen_name=g2c-m365-tui state=not_registered session_id=- task=-\n"));
        QFile extra(base + "/sessions"); QVERIFY(extra.open(QIODevice::Append));
        for (const auto &id : {"gpt2codex"}) {
            extra.write("multiplexer=screen generation=- project_id=" + QByteArray(id)
                + " screen_name=g2c-" + id + " state=not_registered session_id=- task=-\n");
        }
        for (const auto &id : {"k2-smb", "reddit2tg"}) {
            extra.write("multiplexer=screen generation=444 project_id=" + QByteArray(id)
                + " screen_name=g2c-" + id + " state=detached session_id=444.g2c-" + id + " program=anchor exit_code=- exit_signal=- exited_at=- window=0 window_title=g2c-anchor task=Adopted\n");
        }
        extra.close();
        QVERIFY(write(cli, "#!/bin/sh\ncase \"$1/$2\" in\nprojects/list) echo project_dir=/synthetic; echo 'id=kencoder name=KEncoder root=/synthetic/kencoder'; echo 'id=m365-tui name=Missing root=/synthetic/m365-tui';;\nsession/list) if [ \"$4\" != --config ] || [ \"$5\" != \"$(dirname \"$0\")/config.toml\" ]; then echo 'project_id=bridge screen_name=g2c-bridge state=not_registered session_id=- task=Bridge'; else cat \"$(dirname \"$0\")/sessions\"; fi;;\n*) exit 89;;\nesac\n"));
        QVERIFY(QFile::setPermissions(cli, QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner));
        QProcess otherConfig;
        otherConfig.start(cli, {"session", "list", "--program-status", "--config", base + "/other.toml"});
        QVERIFY(otherConfig.waitForFinished(1000));
        QVERIFY(otherConfig.readAllStandardOutput().contains("state=not_registered"));
        Options options; options.bridgeCli = cli; options.config = base + "/config.toml";
        options.queueConfig = base + "/queue.json";
        QVERIFY(write(options.queueConfig, R"({"version":1,"project_roots":{"kencoder":"/a","m365-tui":"/b","gpt2codex":"/c","k2-smb":"/d","reddit2tg":"/e"}})"));
        MemoryBackend backend; MainWindow window(options, &backend); window.show();
        QCOMPARE(window.findChildren<SessionPage *>().size(), 0);
        QCOMPARE(window.findChild<QTabWidget *>()->tabText(0), QString("Queue Worker"));
        QCOMPARE(backend.starts, 0);
        QTRY_COMPARE(window.findChildren<SessionPage *>().size(), 1);
        auto pages = window.findChildren<SessionPage *>();
        QCOMPARE(pages.size(), 1);
        QCOMPARE(pages.front()->session().id, QStringLiteral("kencoder"));
        QCOMPARE(pages.front()->session().windowTitle, QStringLiteral("g2c-kencoder"));
        QCOMPARE(backend.starts, 1); // Memory clients only; no real screen invocation.
        QVERIFY(window.statusBar()->currentMessage().contains("1 · anchor-only"));
        QVERIFY(window.statusBar()->currentMessage().contains(cli));
        auto refresh = [&] {
            window.statusBar()->clearMessage();
            for (auto *action : window.findChildren<QAction *>()) {
                if (action->text() == "Obnovit seznam") { action->trigger(); return; }
            }
            QFAIL("Missing refresh action");
        };
        auto *problem = window.findChild<QLabel *>("queueAllowlistProblem"); QVERIFY(problem);
        QVERIFY(QFile::remove(options.queueConfig)); refresh();
        QTRY_VERIFY(problem->isVisible());
        QTRY_VERIFY(window.statusBar()->currentMessage().contains("1 · anchor-only"));
        QCOMPARE(window.findChildren<SessionPage *>().size(), 1);
        QCOMPARE(backend.starts, 1);
        QVERIFY(write(options.queueConfig, R"({"version":1,"project_roots":{"m365-tui":"/b"}})"));
        refresh(); QTRY_VERIFY(!problem->isVisible());
        QTRY_VERIFY(window.statusBar()->currentMessage().contains("0 · anchor-only"));
        auto *tabs = window.findChild<QTabWidget *>();
        QTRY_COMPARE(window.findChildren<SessionPage *>().size(), 0);
        QCOMPARE(tabs->count(), 2); // Only the pinned monitors remain; no empty project placeholder.
        QCOMPARE(backend.starts, 1); // Removing permission disconnects only the view.
        QVERIFY(backend.last.isNull());
    }
    void queueAllowlistAndExplicitConfig() {
        QTemporaryDir fixture(fixtureTemplate()); QVERIFY(fixture.isValid());
        const auto config = fixture.path() + "/cli.toml";
        Options options; QString error;
        QVERIFY(parseOptions({"gui", "--config", config, "--queue-config", fixture.path() + "/queue.json"}, options, error));
        QCOMPARE(options.config, config);
        QVERIFY(listArguments(options).contains(config));
        const auto attach = attachArguments(options, live);
        QVERIFY(attach.contains(config));
        Options defaults;
        QVERIFY(parseOptions({"gui"}, defaults, error));
        QCOMPARE(defaults.config, defaultBridgeConfig());
        QVERIFY(listArguments(defaults).contains(defaults.config));
        QSet<QString> ids{"preserved"};
        QVERIFY(!queueAllowedProjects(options.queueConfig, ids, error));
        QCOMPARE(ids, QSet<QString>{"preserved"});
        QVERIFY(!error.isEmpty());
        for (const auto &invalid : {QByteArray("broken"), QByteArray(R"({"version":1,"project_roots":{}})"),
                QByteArray(R"({"version":1,"project_roots":{"demo":"relative"}})")}) {
            QFile f(options.queueConfig); QVERIFY(f.open(QIODevice::WriteOnly));
            f.write(invalid); f.close();
            QVERIFY(!queueAllowedProjects(options.queueConfig, ids, error));
            QCOMPARE(ids, QSet<QString>{"preserved"});
        }
    }
    void queueActiveAndHistory() {
        QVERIFY(activeQueueRow(QJsonObject{{"queue_status", "pending"}}));
        QVERIFY(activeQueueRow(QJsonObject{{"queue_status", "approved-project-screen"}}));
        QVERIFY(activeQueueRow(QJsonObject{{"queue_status", "running"}}));
        QVERIFY(activeQueueRow(QJsonObject{{"queue_status", "running"}, {"screen_alive", false}}));
        // A legacy TUI can remain attachable after the Queue job is DONE.
        QVERIFY(!activeQueueRow(QJsonObject{{"queue_status", "done"}, {"screen_alive", true}, {"attachable", true}}));
        QVERIFY(!activeQueueRow(QJsonObject{{"queue_status", "failed"}, {"screen_alive", true}}));
        QVERIFY(!activeQueueRow(QJsonObject{{"screen_alive", true}, {"attachable", true}}));
        QVERIFY(!activeQueueRow(QJsonObject{{"queue_status", "done"}}));
        QVERIFY(!activeQueueRow(QJsonObject{{"queue_status", "rejected"}}));
        QVERIFY(!activeQueueRow(QJsonObject{{"queue_status", "interrupted_unknown"}}));
    }
    void queueIssue9HistoricalAndPartialRows() {
        const auto rows = QJsonDocument::fromJson(R"json([
          {"slot":"q0-185c5ee8cf20","number":0,"project":"gpt2codex","phase":"interrupted_unknown","thread_id":null,"turns_completed":0,"turns_started":0,"last_phase":"issue_accepted","attachable":false},
          {"slot":"q8-1282176b7c79","number":8,"project":"gpt2codex","exit_code":0,"finished_at":1794230779,"phase":"tui_ready","thread_id":"01a1c07c-c85e-77d3-84b8-d4dd19b37394","tui_pid":1827407,"tui_ready":"operator_visual","turns_completed":1,"turns_started":1,"attachable":true}
        ])json").array();
        auto parsed = parseQueueScreenRows(rows);
        QCOMPARE(parsed.rows.size(), 2); QVERIFY(parsed.warnings.isEmpty());
        QCOMPARE(parsed.rows[0].value("phase").toString(), QString("interrupted_unknown"));
        QVERIFY(!parsed.rows[0].value("attachable").toBool());
        QCOMPARE(parseQueueScreenRows(QJsonArray{rows[1]}).rows.size(), 1);
        // Missing optional fields are distinct from malformed identity/types.
        QJsonObject partial{{"slot", "q2-0123456789ab"}, {"number", 2}, {"project", "gpt2codex"}};
        parsed = parseQueueScreenRows(QJsonArray{partial, rows[1]});
        QCOMPARE(parsed.rows.size(), 2); QCOMPARE(parsed.warnings.size(), 2);
        QCOMPARE(parsed.rows[0].value("phase").toString(), QString("metadata_incomplete"));
        QVERIFY(!parsed.rows[0].value("attachable").toBool());
        auto invalid = rows[0].toObject(); invalid.insert("number", 8);
        parsed = parseQueueScreenRows(QJsonArray{invalid, rows[1], false});
        QCOMPARE(parsed.rows.size(), 1); QCOMPARE(parsed.warnings.size(), 2);
        invalid = rows[0].toObject(); invalid.insert("attachable", "true");
        parsed = parseQueueScreenRows(QJsonArray{invalid, rows[1]});
        QCOMPARE(parsed.rows.size(), 1); QVERIFY(parsed.warnings.join('\n').contains("typ"));
        // Quarantine every occurrence; never arbitrarily trust first/last duplicate.
        parsed = parseQueueScreenRows(QJsonArray{rows[0], rows[0], rows[1]});
        QCOMPARE(parsed.rows.size(), 1); QCOMPARE(parsed.warnings.size(), 2);
        QVERIFY(parsed.warnings.join('\n').contains("duplicitní"));
        parsed = parseQueueScreenRows(QJsonArray{rows[0], rows[1], rows[1]});
        QCOMPARE(parsed.rows.size(), 1); QCOMPARE(parsed.rows[0].value("number").toInt(), 0);
    }
    void queueTwoLineListAndVerifiedApproval() {
        QTemporaryDir fixture(fixtureTemplate()); QVERIFY(fixture.isValid());
        const QString base = fixture.path();
        auto write = [](const QString &path, const QByteArray &data) {
            QFile file(path);
            return file.open(QIODevice::WriteOnly | QIODevice::Truncate)
                && file.write(data) == data.size();
        };
        const QString helper = base + "/queue.py";
        const QString cli = base + "/gpt2codex-github-queue.py";
        const QString bridge = base + "/bridge";
        QVERIFY(write(helper, R"PY(import json,sys
assert sys.argv[1:] == ['list']
print('[]')
)PY"));
        QVERIFY(write(cli, R"PY(import sys,json,pathlib
here = pathlib.Path(__file__).parent
args = sys.argv[1:]
if args[:1] == ['--config']: args = args[2:]
if args == ['gui-list']:
    print(json.dumps([{'number':35,'project':'txt-proc','status':'pending','mode':'workspace-write','digest':'a'*64,'title':'Dlouhý úplný název úlohy bez zkracování'}]))
elif args == ['gui-detail','35']:
    print(json.dumps({'number':35,'project':'txt-proc','status':'pending','mode':'workspace-write','digest':'a'*64,'remote_state':'open','title':'Dlouhý úplný název úlohy bez zkracování','task':'Úplné zadání\nDruhý odstavec.'}))
elif args[:2] == ['gui-approve','35']:
    assert args[2:] == ['--digest','a'*64,'--confirmation','SCHVALUJI 35']
    (here/'approved').write_text('yes')
    print('APPROVED #35')
else: sys.exit(88)
)PY"));
        QVERIFY(write(bridge, "#!/bin/sh\ncase \"$1/$2\" in\nprojects/list) echo project_dir=/synthetic;;\nsession/list) echo 'project_id=bridge screen_name=g2c-bridge state=not_registered session_id=- task=Bridge';;\n*) exit 88;;\nesac\n"));
        QVERIFY(QFile::setPermissions(bridge, QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner));
        Options options;
        options.bridgeCli = bridge; options.queueHelper = helper; options.queueOnly = true;
        MemoryBackend backend;
        MainWindow window(options, &backend); window.show();
        auto *page = window.findChild<QueuePage *>(); QVERIFY(page);
        auto *list = page->findChild<QListWidget *>("queueTaskList"); QVERIFY(list);
        QTRY_COMPARE(list->count(), 1);
        QCOMPARE(list->item(0)->text(), QString::fromUtf8("#35    txt-proc    pending\nDlouhý úplný název úlohy bez zkracování"));
        list->setCurrentRow(0);
        auto *body = page->findChild<QPlainTextEdit *>("queueTaskBody"); QVERIFY(body);
        auto *approval = page->findChild<QCheckBox *>("queueTaskApproval"); QVERIFY(approval);
        auto *confirm = page->findChild<QPushButton *>("queueTaskConfirm"); QVERIFY(confirm);
        QTRY_COMPARE(body->toPlainText(), QString::fromUtf8("Úplné zadání\nDruhý odstavec."));
        QVERIFY(approval->isEnabled()); QVERIFY(!confirm->isEnabled());
        approval->setChecked(true);
        QVERIFY(confirm->isEnabled());
        confirm->click();
        QTRY_VERIFY(QFileInfo::exists(base + "/approved"));
        QCOMPARE(backend.starts, 0); // Local GUI approval never launches/attaches Codex.
    }
    void queueDefaultsAndOverrides() {
        Options options; QString error;
        QVERIFY2(parseOptions({"gui"}, options, error), qPrintable(error));
        QVERIFY(QFileInfo(options.bridgeCli).isExecutable());
        QVERIFY(options.queueHelper.endsWith("gpt2codex_queue_screen.py"));
        QTemporaryDir fixture(fixtureTemplate()); QVERIFY(fixture.isValid());
        const QString helper = fixture.path() + "/gpt2codex_queue_screen.py";
        const QString queue = fixture.path() + "/gpt2codex-github-queue.py";
        QFile file(helper); QVERIFY(file.open(QIODevice::WriteOnly)); file.close();
        QFile cliFile(queue); QVERIFY(cliFile.open(QIODevice::WriteOnly)); cliFile.close();
        Options overrideOptions;
        QVERIFY(parseOptions({"gui", "--bridge-cli", options.bridgeCli,
            "--queue-helper", helper, "--queue-only"}, overrideOptions, error));
        QCOMPARE(overrideOptions.queueHelper, QFileInfo(helper).canonicalFilePath());
        QCOMPARE(overrideOptions.bridgeCli, options.bridgeCli);
        QVERIFY(overrideOptions.queueOnly);
        Options wrongHelper;
        QVERIFY(!parseOptions({"gui", "--queue-helper", queue, "--queue-only"}, wrongHelper, error));
        QVERIFY(error.contains("gpt2codex_queue_screen.py"));
        const QString checkout = fixture.path() + "/checkout";
        QVERIFY(QDir().mkpath(checkout + "/.tmp"));
        QVERIFY(QDir().mkpath(checkout + "/.log"));
        const QString config = fixture.path() + "/queue.json";
        const auto bytes = QJsonDocument(QJsonObject{{"version", 1},
            {"project_roots", QJsonObject{{"gpt2codex", checkout}}}}).toJson();
        QFile configFile(config); QVERIFY(configFile.open(QIODevice::WriteOnly));
        QCOMPARE(configFile.write(bytes), qint64(bytes.size())); configFile.close();
        Options installedOptions;
        QVERIFY2(parseOptions({"gui", "--queue-helper", helper, "--queue-config", config,
            "--queue-only"}, installedOptions, error), qPrintable(error));
        QCOMPARE(installedOptions.queueRepoRoot, QFileInfo(checkout).canonicalFilePath());
        QVERIFY(installedOptions.queueRootError.isEmpty());
        Options duplicate;
        QVERIFY(!parseOptions({"gui", "--bridge-cli", options.bridgeCli, "--queue-only", "--queue-only"}, duplicate, error));
    }
    void queueInvalidDetailCannotApproveOrAttach() {
        QTemporaryDir fixture(fixtureTemplate()); QVERIFY(fixture.isValid());
        const QString base = fixture.path();
        auto write = [](const QString &path, const QByteArray &data) {
            QFile file(path);
            return file.open(QIODevice::WriteOnly | QIODevice::Truncate)
                && file.write(data) == data.size();
        };
        QVERIFY(write(base + "/queue.py", "print('[]')\n"));
        QVERIFY(write(base + "/gpt2codex-github-queue.py", R"PY(import sys,json
args = sys.argv[1:]
if args[:1] == ['--config']: args = args[2:]
if args == ['gui-list']:
    print(json.dumps([{'number':35,'project':'txt-proc','status':'pending','mode':'workspace-write','title':'Test'}]))
else: sys.exit(2)
)PY"));
        Options options; options.queueOnly = true; options.queueHelper = base + "/queue.py";
        MemoryBackend backend; MainWindow window(options, &backend); window.show();
        auto *page = window.findChild<QueuePage *>(); QVERIFY(page);
        auto *list = page->findChild<QListWidget *>("queueTaskList"); QVERIFY(list);
        QTRY_COMPARE(list->count(), 1);
        list->setCurrentRow(0);
        auto *approval = page->findChild<QCheckBox *>("queueTaskApproval"); QVERIFY(approval);
        auto *confirm = page->findChild<QPushButton *>("queueTaskConfirm"); QVERIFY(confirm);
        auto *body = page->findChild<QPlainTextEdit *>("queueTaskBody"); QVERIFY(body);
        QTRY_VERIFY(body->toPlainText().contains("nepodařilo ověřit"));
        QVERIFY(!approval->isEnabled()); QVERIFY(!confirm->isEnabled());
        QCOMPARE(backend.starts, 0);
    }
    void markerHelperAndWatcherThreeSessions() {
        using namespace g2cattention;
        using namespace g2cmarkertest;
        Fixture fixture; QVERIFY2(fixture.setup(), qPrintable(fixture.error));
        const auto a = fixture.start("alpha", "screen"), b = fixture.start("beta", "tmux"), c = fixture.start("gamma", "screen");
        QVERIFY2(a && b && c, qPrintable(fixture.error));
        MarkerAttentionSource source(fixture.runtime);
        auto runA = source.binding(a->identity); QVERIFY(runA); QVERIFY(runA->waitingOnly);
        const QString registry = fixture.state + "/gpt2codex/screen-sessions/alpha.json";
        const auto registryBefore = bytes(registry);
        const QString listingPath = fixture.base + "/listing";
        QVERIFY(write(listingPath, fixture.listing.toUtf8()));
        QByteArray roots = "project_dir=" + (fixture.base + "/repos").toUtf8() + '\n';
        for (const auto &actor : fixture.actors) {
            const auto project = actor->identity.project;
            roots += "id=" + project.toUtf8() + " name=Fixture root=" + (fixture.base + "/repos/" + project).toUtf8() + '\n';
        }
        QVERIFY(write(fixture.base + "/roots", roots));
        const QString guiCli = fixture.base + "/gui-cli";
        QVERIFY(write(guiCli, "#!/bin/sh\nbase=${0%/*}\ncase \"$1/$2\" in\nprojects/list) /bin/cat \"$base/roots\";;\nsession/list) /bin/cat \"$base/listing\";;\n*) exit 89;;\nesac\n"));
        QVERIFY(QFile::setPermissions(guiCli, QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner));
        // Test-only queue allowlist; never depend on the operator's config.
        QJsonObject queueRoots;
        for (const auto &actor : fixture.actors) {
            const auto project = actor->identity.project;
            queueRoots.insert(project, fixture.base + "/repos/" + project);
        }
        const QString queueConfig = fixture.base + "/queue.json";
        QVERIFY(write(queueConfig, QJsonDocument(QJsonObject{{"version", 1}, {"project_roots", queueRoots}}).toJson(QJsonDocument::Compact)));
        Options options; options.bridgeCli = guiCli; options.queueConfig = queueConfig; MemoryBackend backend;
        QString waitB;
        Ticket oldTicket;
        {
            MainWindow window(options, &backend, &source);
            auto *tabs = window.findChild<QTabWidget *>(); QVERIFY(tabs);
            QTRY_COMPARE(tabs->count(), 5); QTRY_VERIFY(window.attentionTicket(a->identity.slot()).valid());
            QCOMPARE(backend.starts, 3);
            const int starts = backend.starts, creates = backend.creates;
            QWidget *selected = tabs->currentWidget();
            const auto waitA = fixture.mark(a); QVERIFY(marker::nonce(waitA));
            QTRY_COMPARE_WITH_TIMEOUT(window.attentionView(a->identity.slot()).state, State::Waiting, 1000);
            QCOMPARE(window.attentionView(b->identity.slot()).state, State::Unknown);
            waitB = fixture.mark(b); QVERIFY(marker::nonce(waitB));
            QTRY_COMPARE_WITH_TIMEOUT(window.attentionView(b->identity.slot()).state, State::Waiting, 1000);
            const auto waitC = fixture.mark(c); QVERIFY(marker::nonce(waitC));
            QTRY_COMPARE_WITH_TIMEOUT(window.attentionView(c->identity.slot()).state, State::Waiting, 1000);
            QCOMPARE(fixture.clear(a, waitA), 0);
            QTRY_COMPARE_WITH_TIMEOUT(window.attentionView(a->identity.slot()).state, State::Unknown, 1000);
            const auto questionB = fixture.mark(a); QVERIFY(marker::nonce(questionB)); QVERIFY(questionB != waitA);
            QTRY_COMPARE_WITH_TIMEOUT(window.attentionView(a->identity.slot()).state, State::Waiting, 1000);
            const auto beforeLateClear = bytes(a->directory + "/waiting.json");
            QVERIFY(fixture.clear(a, waitA) != 0);
            QCOMPARE(bytes(a->directory + "/waiting.json"), beforeLateClear);
            QCOMPARE(window.attentionView(a->identity.slot()).state, State::Waiting);
            // Temporary half-writes do not replace the complete waiting record.
            QVERIFY(write(a->directory + "/.tmp-owned-fixture", "{partial"));
            source.reload(); QCOMPARE(window.attentionView(a->identity.slot()).state, State::Waiting);
            oldTicket = window.attentionTicket(a->identity.slot());
            QCOMPARE(backend.starts, starts); QCOMPARE(backend.creates, creates);
            QCOMPARE(tabs->currentWidget(), selected); // No focus/tab takeover.
            const int index = tabs->indexOf(qobject_cast<SessionPage *>(tabs->widget(1)));
            QVERIFY(tabs->tabToolTip(index).contains("uživatelský vstup"));
        }
        {
            MarkerAttentionSource reopened(fixture.runtime);
            MainWindow restarted(options, &backend, &reopened);
            QTRY_COMPARE_WITH_TIMEOUT(restarted.attentionView(a->identity.slot()).state, State::Waiting, 1500);
            QVERIFY(!(restarted.attentionTicket(a->identity.slot()) == oldTicket));
            const int starts = backend.starts;
            fixture.stop(c); // Only the owned synthetic program, no server/Codex.
            QVERIFY(QFileInfo::exists(c->directory + "/waiting.json"));
            QTRY_COMPARE_WITH_TIMEOUT(restarted.attentionView(c->identity.slot()).state, State::Unknown, 4500);
            QCOMPARE(restarted.attentionView(a->identity.slot()).state, State::Waiting);
            QCOMPARE(restarted.attentionView(b->identity.slot()).state, State::Waiting);
            QCOMPARE(backend.starts, starts);
            // A changed generation cannot preserve a stale waiting marker.
            auto wrong = b->binding; wrong.insert("codex_start_ticks", "1");
            QVERIFY(json(b->directory + "/binding.json", wrong));
            QTRY_COMPARE_WITH_TIMEOUT(restarted.attentionView(b->identity.slot()).state, State::Unknown, 1000);
            QVERIFY(json(b->directory + "/binding.json", b->binding));
            QTRY_COMPARE_WITH_TIMEOUT(restarted.attentionView(b->identity.slot()).state, State::Waiting, 1000);
            QCOMPARE(fixture.clear(b, waitB), 0);
            QTRY_COMPARE_WITH_TIMEOUT(restarted.attentionView(b->identity.slot()).state, State::Unknown, 1000);
        }
        QCOMPARE(bytes(registry), registryBefore);
        QVERIFY(bytes(a->directory + "/instructions.md").contains("attention mark"));
    }
    void markerUnsafeFilesStayUnknown_data() {
        QTest::addColumn<QString>("scenario");
        for (const char *name : {"binding-symlink", "marker-symlink", "runtime-symlink", "parent-symlink", "run-mode", "binding-mode", "marker-mode",
                "fifo", "oversized", "unknown-field", "duplicate-key", "bad-namespace", "wrong-run", "wrong-session", "hardlink", "marker-binding"}) {
            QTest::newRow(name) << QString::fromLatin1(name);
        }
    }
    void markerUnsafeFilesStayUnknown() {
        using namespace g2cattention;
        using namespace g2cmarkertest;
        QFETCH(QString, scenario);
        Fixture fixture; QVERIFY2(fixture.setup(), qPrintable(fixture.error));
        const auto actor = fixture.start("alpha", "screen"); QVERIFY2(actor, qPrintable(fixture.error));
        QVERIFY(marker::nonce(fixture.mark(actor)));
        QString runtime = fixture.runtime;
        const QString bindingPath = actor->directory + "/binding.json", markerPath = actor->directory + "/waiting.json";
        if (scenario == "binding-symlink" || scenario == "marker-symlink") {
            const auto path = scenario == "binding-symlink" ? bindingPath : markerPath;
            QVERIFY(QFile::rename(path, path + ".saved")); QVERIFY(QFile::link(path + ".saved", path));
        } else if (scenario == "runtime-symlink") {
            QVERIFY(QFile::link(runtime, fixture.base + "/alias")); runtime = fixture.base + "/alias";
        } else if (scenario == "parent-symlink") {
            QVERIFY(QDir().rename(runtime + "/gpt2codex", runtime + "/saved"));
            QVERIFY(QFile::link(runtime + "/saved", runtime + "/gpt2codex"));
        } else if (scenario.endsWith("-mode")) {
            const auto path = scenario == "run-mode" ? actor->directory : scenario == "binding-mode" ? bindingPath : markerPath;
            QVERIFY(QFile::setPermissions(path, QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ReadGroup
                | (scenario == "run-mode" ? QFileDevice::ExeOwner : QFileDevice::Permissions{})));
        } else if (scenario == "hardlink") {
            QCOMPARE(::link(QFile::encodeName(markerPath).constData(), QFile::encodeName(markerPath + ".linked").constData()), 0);
        } else if (scenario == "marker-binding") {
            auto object = QJsonDocument::fromJson(bytes(markerPath)).object();
            auto wrong = actor->binding; wrong.insert("project_id", "other"); object.insert("binding", wrong);
            QVERIFY(json(markerPath, object));
        } else if (scenario == "fifo") {
            QVERIFY(QFile::remove(markerPath)); QCOMPARE(::mkfifo(QFile::encodeName(markerPath).constData(), 0600), 0);
        } else if (scenario == "oversized") { QVERIFY(write(markerPath, QByteArray(4097, 'x'))); }
        else if (scenario == "unknown-field") {
            auto object = QJsonDocument::fromJson(bytes(markerPath)).object(); object.insert("prompt", "synthetic-private-sentinel"); QVERIFY(json(markerPath, object));
        } else if (scenario == "duplicate-key") {
            const auto original = bytes(markerPath); QVERIFY(write(markerPath, "{\"status\":\"waiting\"," + original.mid(1)));
        } else {
            auto wrong = actor->binding;
            if (scenario == "bad-namespace") { wrong.insert("pid_namespace", "1"); }
            if (scenario == "wrong-run") { wrong.insert("run_nonce", QString(32, 'a')); }
            if (scenario == "wrong-session") { wrong.insert("session_id", "1.g2c-alpha"); }
            QVERIFY(json(bindingPath, wrong));
        }
        MarkerAttentionSource source(runtime);
        QVERIFY(!source.binding(actor->identity));
    }
    void markerWatcherRecoversRecreatedDirectory() {
        using namespace g2cattention;
        using namespace g2cmarkertest;
        Fixture fixture; QVERIFY2(fixture.setup(), qPrintable(fixture.error));
        const auto actor = fixture.start("alpha", "screen"); QVERIFY2(actor, qPrintable(fixture.error));
        QVERIFY(marker::nonce(fixture.mark(actor)));
        MarkerAttentionSource source(fixture.runtime);
        const auto original = source.binding(actor->identity); QVERIFY(original);
        const QString path = fixture.runtime + "/gpt2codex/attention";
        QVERIFY(QDir().rename(path, path + ".saved"));
        QTRY_VERIFY_WITH_TIMEOUT(!source.binding(actor->identity), 1000);
        QVERIFY(QDir().rename(path + ".saved", path));
        QTRY_VERIFY_WITH_TIMEOUT(source.binding(actor->identity).has_value(), 1000);
        QVERIFY(source.binding(actor->identity)->sourceEpoch != original->sourceEpoch);
        // Simulate a lost file notification; the lightweight revalidation repairs it.
        const auto wait = QJsonDocument::fromJson(bytes(actor->directory + "/waiting.json")).object().value("wait_nonce").toString();
        QCOMPARE(fixture.clear(actor, wait), 0);
        source.reload();
        Manager manager;
        const auto ticket = manager.observe(actor->identity, source.binding(actor->identity), true);
        source.watch(ticket, [&](const Event &event) { manager.apply(event, 0); });
        QCOMPARE(manager.view(actor->identity.slot()).state, State::Unknown);
        source.unwatch(ticket);
    }
    void attentionThreeParallelSessionsAndPendingPriority() {
        using namespace g2cattention;
        Manager manager;
        const auto a = manager.observe(attentionId(), attentionRun(), true);
        const auto b = manager.observe(attentionId("beta", "tmux"), attentionRun(), true);
        const auto c = manager.observe(attentionId("gamma"), attentionRun(), true);
        auto waiting = attentionEvent(b);
        waiting.pending = {{"question", WaitReason::Input}, {"command", WaitReason::Approval}};
        QVERIFY(manager.apply(attentionEvent(c, 1, Kind::Snapshot, Phase::Completed), 0));
        QVERIFY(manager.apply(waiting, 0));
        QVERIFY(manager.apply(attentionEvent(a), 0));
        QCOMPARE(manager.view(a.identity.slot()).state, State::Working);
        QCOMPARE(manager.view(b.identity.slot()).state, State::Waiting);
        QCOMPARE(manager.view(c.identity.slot()).state, State::Completed);
        QVERIFY(description(manager.view(b.identity.slot())).contains("schválení a uživatelský vstup"));
        waiting.sequence = 2; waiting.kind = Kind::Update;
        waiting.pending.removeFirst(); waiting.phase = Phase::Completed;
        QVERIFY(manager.apply(waiting, 1));
        QCOMPARE(manager.view(b.identity.slot()).state, State::Waiting); // Pending wins over completion.
        waiting.sequence = 3; waiting.pending.clear();
        QVERIFY(manager.apply(waiting, 2));
        QCOMPARE(manager.view(b.identity.slot()).state, State::Completed);
    }
    void attentionWaitingOnlyMarkerContract() {
        using namespace g2cattention;
        Manager manager;
        const auto ticket = manager.observe(attentionId(), attentionRun(true), true);
        QVERIFY(ticket.valid()); // No app-server/thread API is needed for this capability.
        Event marker; marker.ticket = ticket; marker.sequence = 1;
        marker.kind = Kind::WaitingSnapshot; marker.waiting = true;
        QVERIFY(manager.apply(marker, 0));
        QCOMPARE(manager.view(ticket.identity.slot()).state, State::Waiting);
        QVERIFY(!manager.view(ticket.identity.slot()).approval);
        marker.sequence = 2; marker.kind = Kind::WaitingChanged; marker.waiting = false;
        QVERIFY(manager.apply(marker, 1));
        QCOMPARE(manager.view(ticket.identity.slot()).state, State::Unknown);
        QCOMPARE(manager.view(ticket.identity.slot()).reason, Reason::WaitingCleared);
        QVERIFY(manager.apply(attentionEvent(ticket, 3, Kind::Update, Phase::Completed), 2));
        QCOMPARE(manager.view(ticket.identity.slot()).state, State::Unknown); // Cannot promote a marker to green.
    }
    void attentionIdentityAndRunChangesRejectOldEvents_data() {
        QTest::addColumn<QString>("field");
        for (const char *field : {"project", "mux", "session", "generation", "run", "source", "thread"}) {
            QTest::newRow(field) << QString::fromLatin1(field);
        }
    }
    void attentionIdentityAndRunChangesRejectOldEvents() {
        using namespace g2cattention;
        QFETCH(QString, field);
        Manager manager;
        auto identity = attentionId(); auto run = attentionRun();
        const auto old = manager.observe(identity, run, true);
        QVERIFY(manager.apply(attentionEvent(old), 0));
        if (field == "project") { identity.project = "other"; manager.remove(old.identity.slot()); }
        if (field == "mux") { identity.multiplexer = "tmux"; manager.remove(old.identity.slot()); }
        if (field == "session") { identity.sessionId = "102.g2c-alpha"; }
        if (field == "generation") { identity.generation = "12"; }
        if (field == "run") { run.nonce = "new-run"; }
        if (field == "source") { run.sourceEpoch = "new-source"; }
        if (field == "thread") { run.threadId = "new-thread"; }
        const auto current = manager.observe(identity, run, true);
        QVERIFY(!(old == current));
        QVERIFY(!manager.apply(attentionEvent(old, 2, Kind::Update, Phase::Completed), 1));
        QCOMPARE(manager.view(identity.slot()).state, State::Unknown);
        QVERIFY(manager.apply(attentionEvent(current), 2));
        QCOMPARE(manager.view(identity.slot()).state, State::Working);
    }
    void attentionReconnectRestartAndSnapshotBarrier() {
        using namespace g2cattention;
        const auto identity = attentionId(); const auto run = attentionRun();
        Manager manager;
        const auto old = manager.observe(identity, run, true);
        QVERIFY(manager.apply(attentionEvent(old), 0));
        manager.observe(identity, run, false);
        QVERIFY(!manager.apply(attentionEvent(old, 2, Kind::Update), 1));
        QCOMPARE(manager.view(identity.slot()).state, State::Unknown);
        const auto current = manager.observe(identity, run, true);
        QVERIFY(!(old == current));
        QVERIFY(!manager.apply(attentionEvent(old, 3, Kind::Snapshot), 2));
        QVERIFY(manager.apply(attentionEvent(current), 3));
        // A late snapshot cannot roll the new state back.
        QVERIFY(!manager.apply(attentionEvent(current, 2, Kind::Snapshot, Phase::Completed), 4));
        QCOMPARE(manager.view(identity.slot()).state, State::Working);
        Manager restarted;
        const auto fresh = restarted.observe(identity, run, true);
        QVERIFY(!(fresh == current));
        QCOMPARE(restarted.view(identity.slot()).state, State::Unknown);
        QVERIFY(!restarted.apply(attentionEvent(current, 2, Kind::Update), 5));
        QVERIFY(restarted.apply(attentionEvent(fresh, 7, Kind::Snapshot, Phase::Completed), 6));
        QCOMPARE(restarted.view(identity.slot()).state, State::Completed);
    }
    void attentionDuplicateOldTurnAndLostSource() {
        using namespace g2cattention;
        Manager manager;
        const auto ticket = manager.observe(attentionId(), attentionRun(), true);
        QVERIFY(manager.apply(attentionEvent(ticket), 0));
        QVERIFY(!manager.apply(attentionEvent(ticket), 100)); // Duplicate doesn't refresh freshness.
        auto next = attentionEvent(ticket, 2, Kind::TurnStarted);
        next.turnId = "synthetic-turn-2"; next.turnOrdinal = 2;
        QVERIFY(manager.apply(next, 1));
        QVERIFY(!manager.apply(attentionEvent(ticket, 3, Kind::Update, Phase::Completed), 2));
        QCOMPARE(manager.view(ticket.identity.slot()).state, State::Working);
        next.sequence = 4; next.kind = Kind::Update; next.pending = {{"question", WaitReason::Input}};
        QVERIFY(manager.apply(next, 3));
        QCOMPARE(manager.view(ticket.identity.slot()).state, State::Waiting);
        auto lost = next; lost.sequence = 5; lost.kind = Kind::SourceLost;
        QVERIFY(manager.apply(lost, 4));
        QCOMPARE(manager.view(ticket.identity.slot()).reason, Reason::Lost);
        next.sequence = 6;
        QVERIFY(!manager.apply(next, 5)); // No automatic recovery without a new ticket.
    }
    void attentionIncompleteInvalidAndGapEvidence_data() {
        QTest::addColumn<QString>("failure");
        for (const char *name : {"partial", "gap", "no-snapshot", "unknown-kind", "unknown-phase", "duplicate-request", "oversized-id", "text-id", "wrong-turn"}) {
            QTest::newRow(name) << QString::fromLatin1(name);
        }
    }
    void attentionIncompleteInvalidAndGapEvidence() {
        using namespace g2cattention;
        QFETCH(QString, failure);
        Manager manager;
        const auto ticket = manager.observe(attentionId(), attentionRun(), true);
        auto event = attentionEvent(ticket);
        if (failure != "no-snapshot") { QVERIFY(manager.apply(event, 0)); event.sequence = 2; }
        event.kind = Kind::Update;
        if (failure == "partial") { event.complete = false; }
        if (failure == "gap") { event.sequence = 4; }
        if (failure == "unknown-kind") { event.kind = static_cast<Kind>(99); }
        if (failure == "unknown-phase") { event.phase = static_cast<Phase>(99); }
        if (failure == "duplicate-request") { event.pending = {{"same", WaitReason::Input}, {"same", WaitReason::Approval}}; }
        if (failure == "oversized-id") { event.turnId = QString(129, 'x'); }
        if (failure == "text-id") { event.turnId = "<synthetic-private-content>"; }
        if (failure == "wrong-turn") { event.turnId = "different-id-same-ordinal"; }
        QVERIFY(manager.apply(event, 1));
        QCOMPARE(manager.view(ticket.identity.slot()).state, State::Unknown);
        QVERIFY(!description(manager.view(ticket.identity.slot())).contains("synthetic-private"));
        event = attentionEvent(ticket, 5, Kind::Snapshot);
        QVERIFY(!manager.apply(event, 2));
    }
    void attentionFreshnessAndFailureNeverImplyCompletion() {
        using namespace g2cattention;
        for (Phase phase : {Phase::Idle, Phase::Failed, Phase::Interrupted}) {
            Manager manager;
            const auto ticket = manager.observe(attentionId(), attentionRun(), true);
            auto event = attentionEvent(ticket, 1, Kind::Snapshot, phase);
            if (phase == Phase::Idle) { event.turnId.clear(); event.turnOrdinal = 0; }
            QVERIFY(manager.apply(event, 0));
            QCOMPARE(manager.view(ticket.identity.slot()).state, State::Unknown);
        }
        Manager manager;
        const auto ticket = manager.observe(attentionId(), attentionRun(), true);
        QVERIFY(manager.apply(attentionEvent(ticket), 0));
        QVERIFY(!manager.apply(attentionEvent(ticket), 14999));
        QCOMPARE(manager.expire(15000), QStringList{ticket.identity.slot()});
        QCOMPARE(manager.view(ticket.identity.slot()).reason, Reason::Expired);
        QVERIFY(!manager.apply(attentionEvent(ticket, 2, Kind::Heartbeat), 15001));
        manager.observe(ticket.identity, std::nullopt, true);
        QCOMPARE(manager.view(ticket.identity.slot()).reason, Reason::Unavailable);
        Manager live;
        const auto current = live.observe(attentionId(), attentionRun(), true);
        QVERIFY(live.apply(attentionEvent(current), 0));
        QVERIFY(live.apply(attentionEvent(current, 2, Kind::Heartbeat), 14000));
        QVERIFY(live.expire(15000).isEmpty());
        QCOMPARE(live.expire(29000), QStringList{current.identity.slot()});
    }
    void attentionUnverifiedIdentityCannotSubscribe() {
        using namespace g2cattention;
        for (int invalid = 0; invalid < 6; ++invalid) {
            auto id = attentionId();
            if (invalid == 0) { id.project = "../other"; }
            if (invalid == 1) { id.project = "UPPER"; }
            if (invalid == 2) { id.multiplexer = "other"; }
            if (invalid == 3) { id.sessionId = "-"; }
            if (invalid == 4) { id.generation = "0"; }
            if (invalid == 5) { id.generation = " 11"; }
            Manager manager;
            const auto ticket = manager.observe(id, attentionRun(), true);
            QVERIFY(!ticket.valid());
            QCOMPARE(manager.view(id.slot()).reason, Reason::Identity);
            QVERIFY(!manager.apply(attentionEvent(ticket), 0));
        }
    }
    void attentionIndicatorGeometryDpiAndRtl() {
        using namespace g2cattention;
        const auto previousDirection = QGuiApplication::layoutDirection();
        QTabWidget tabs; QWidget page; tabs.addTab(&page, "Original");
        QImage artwork(20, 20, QImage::Format_ARGB32); artwork.fill(Qt::blue);
        const int oldHeight = tabs.tabBar()->sizeHint().height();
        for (auto direction : {Qt::LeftToRight, Qt::RightToLeft}) {
            QGuiApplication::setLayoutDirection(direction);
            for (bool dark : {false, true}) {
                QPalette palette = tabs.palette();
                palette.setColor(QPalette::WindowText, dark ? Qt::white : Qt::black); tabs.setPalette(palette);
                for (State state : {State::Unknown, State::Working, State::Waiting, State::Completed}) {
                    const auto icon = g2ctabs::tabIcon(artwork, false, &tabs, {state, Reason::None});
                    for (qreal dpr : {1.0, 2.0}) {
                        const auto pixmap = icon.pixmap(g2ctabs::decorationSize(), dpr, QIcon::Normal, QIcon::Off);
                        const auto image = pixmap.toImage();
                        const int dotX = qRound((direction == Qt::LeftToRight ? 24 : 4) * dpr);
                        const int iconX = qRound((direction == Qt::LeftToRight ? 10 : 18) * dpr);
                        QCOMPARE(image.pixelColor(dotX, qRound(10 * dpr)), g2ctabs::indicatorColor(state));
                        QCOMPARE(image.pixelColor(iconX, qRound(10 * dpr)), QColor(Qt::blue));
                        QVERIFY(opaqueBounds(image).width() <= qRound(28 * dpr));
                    }
                    tabs.setIconSize(g2ctabs::decorationSize()); tabs.setTabIcon(0, icon);
                    QVERIFY(tabs.tabBar()->sizeHint().height() <= oldHeight + 8);
                }
            }
        }
        QGuiApplication::setLayoutDirection(previousDirection);
    }
    void attentionSameProjectSeparateMultiplexers() {
        using namespace g2cattention;
        Manager manager;
        const auto screen = manager.observe(attentionId(), attentionRun(), true);
        const auto tmux = manager.observe(attentionId("alpha", "tmux"), attentionRun(), true);
        QVERIFY(manager.apply(attentionEvent(screen), 0));
        auto wait = attentionEvent(tmux); wait.pending = {{"question", WaitReason::Input}};
        QVERIFY(manager.apply(wait, 0));
        QTabWidget tabs; QWidget first, second;
        tabs.addTab(&first, "Alpha [screen]"); tabs.addTab(&second, "Alpha [tmux]");
        g2ctabs::Presentation presentation;
        for (int refresh = 0; refresh < 2; ++refresh) {
            presentation.apply(&tabs, &first, "alpha", "screen", "Alpha [screen]", "Task", manager.view(screen.identity.slot()));
            presentation.apply(&tabs, &second, "alpha", "tmux", "Alpha [tmux]", "Task", manager.view(tmux.identity.slot()));
            QCOMPARE(tabs.tabIcon(0).pixmap(g2ctabs::decorationSize()).toImage().pixelColor(24, 10), g2ctabs::indicatorColor(State::Working));
            QCOMPARE(tabs.tabIcon(1).pixmap(g2ctabs::decorationSize()).toImage().pixelColor(24, 10), g2ctabs::indicatorColor(State::Waiting));
            QVERIFY(first.accessibleDescription().contains("Pracuje"));
            QVERIFY(second.accessibleDescription().contains("uživatelský vstup"));
            presentation.invalidateIcons();
        }
    }
    void attentionWindowCallbacksArePresentationOnly() {
        using namespace g2cattention;
        QTemporaryDir fixture(fixtureTemplate()); QVERIFY(fixture.isValid());
        const QString base = QDir(fixture.path()).canonicalPath();
        auto write = [](const QString &name, const QByteArray &data) {
            QFile file(name); return file.open(QIODevice::WriteOnly | QIODevice::Truncate) && file.write(data) == data.size();
        };
        QByteArray roots = "project_dir=" + (base + "/repos").toUtf8() + '\n';
        for (const QString &project : {QStringLiteral("gpt2codex"), QStringLiteral("alpha"), QStringLiteral("beta"), QStringLiteral("gamma")}) {
            const QString root = base + "/repos/" + project; QVERIFY(QDir().mkpath(root));
            roots += "id=" + project.toUtf8() + " name=Fixture root=" + root.toUtf8() + '\n';
            QImage icon(20, 20, QImage::Format_ARGB32); icon.fill(Qt::blue);
            QVERIFY(icon.save(root + "/.icon.png", "PNG"));
        }
        QVERIFY(write(base + "/roots", roots));
        QByteArray listing = "multiplexer=screen generation=- project_id=bridge screen_name=g2c-bridge state=not_registered session_id=- task=Bridge\n";
        const QList<Identity> ids{attentionId(), attentionId("beta", "tmux"), attentionId("gamma")};
        FakeAttentionSource source;
        for (const auto &id : ids) {
            source.setBinding(id, attentionRun());
            listing += "multiplexer=" + id.multiplexer.toUtf8() + " generation=" + id.generation.toUtf8()
                + " project_id=" + id.project.toUtf8() + " screen_name=g2c-" + id.project.toUtf8()
                + " state=detached session_id=" + id.sessionId.toUtf8()
                + (id.multiplexer == "screen"
                    ? " program=running exit_code=- exit_signal=- exited_at=- window=0 window_title=g2c-" + id.project.toUtf8()
                    : " program=running exit_code=- exit_signal=- exited_at=- window=- window_title=-")
                + " task=Original\n";
        }
        QVERIFY(write(base + "/sessions", listing));
        const QString cli = base + "/fixture-cli";
        QVERIFY(write(cli, "#!/bin/sh\nbase=${0%/*}\nprintf '%s/%s\\n' \"$1\" \"$2\" >> \"$base/calls\"\ncase \"$1/$2\" in\nprojects/list) /bin/cat \"$base/roots\";;\nsession/list) /bin/cat \"$base/sessions\";;\n*) exit 89;;\nesac\n"));
        QVERIFY(QFile::setPermissions(cli, QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner));
        QJsonObject queueRoots;
        for (const auto &project : {QStringLiteral("gpt2codex"), QStringLiteral("alpha"), QStringLiteral("beta"), QStringLiteral("gamma")}) {
            queueRoots.insert(project, base + "/repos/" + project);
        }
        const QString queueConfig = base + "/queue.json";
        QVERIFY(write(queueConfig, QJsonDocument(QJsonObject{{"version", 1}, {"project_roots", queueRoots}}).toJson(QJsonDocument::Compact)));
        Options options; options.bridgeCli = cli; options.config = base + "/config"; options.queueConfig = queueConfig;
        MemoryBackend backend;
        AttentionSource::Delivery queued;
        Ticket original;
        {
            MainWindow window(options, &backend, &source);
            auto *tabs = window.findChild<QTabWidget *>(); QVERIFY(tabs);
            QTRY_COMPARE(tabs->count(), 5);
            QTRY_VERIFY(tabs->tabToolTip(1).contains(base + "/repos/alpha"));
            QCOMPARE(backend.starts, 3); QCOMPARE(source.subscriptions(), 3);
            original = window.attentionTicket(ids[0].slot()); QVERIFY(original.valid());
            queued = source.queued(original); QVERIFY(bool(queued));
            const auto waitingTicket = window.attentionTicket(ids[1].slot());
            const auto completeTicket = window.attentionTicket(ids[2].slot());
            auto waiting = attentionEvent(waitingTicket); waiting.pending = {{"question", WaitReason::Input}};
            const int starts = backend.starts, creates = backend.creates;
            const auto lastTerminal = backend.last;
            QWidget *const selected = tabs->currentWidget();
            source.send(attentionEvent(completeTicket, 1, Kind::Snapshot, Phase::Completed));
            source.send(attentionEvent(original)); source.send(waiting);
            // No event-loop/poll delay: all three indicators change in delivery.
            for (int i = 0; i < ids.size(); ++i) {
                const auto state = i == 0 ? State::Working : i == 1 ? State::Waiting : State::Completed;
                const auto image = tabs->tabIcon(i + 1).pixmap(g2ctabs::decorationSize()).toImage();
                QCOMPARE(image.pixelColor(24, 10), g2ctabs::indicatorColor(state));
                QCOMPARE(image.pixelColor(10, 10), QColor(Qt::blue));
                QVERIFY(tabs->tabToolTip(i + 1).contains(base + "/repos/" + ids[i].project));
            }
            QCOMPARE(tabs->currentWidget(), selected); QCOMPARE(backend.starts, starts);
            QCOMPARE(backend.creates, creates); QCOMPARE(backend.last, lastTerminal);
            auto *alpha = qobject_cast<SessionPage *>(tabs->widget(1)); QVERIFY(alpha);
            auto *beta = qobject_cast<SessionPage *>(tabs->widget(2)); QVERIFY(beta);
            auto *gamma = qobject_cast<SessionPage *>(tabs->widget(3)); QVERIFY(gamma);
            tabs->tabBar()->moveTab(3, 1);
            tabs->setCurrentWidget(beta);
            QCOMPARE(window.attentionView(ids[1].slot()).state, State::Waiting);
            QVERIFY(tabs->tabToolTip(tabs->indexOf(beta)).contains("Čeká"));
            QAction *refresh = nullptr;
            for (auto *action : window.findChildren<QAction *>()) { if (action->text() == "Obnovit seznam") { refresh = action; } }
            QVERIFY(refresh);
            QVERIFY(write(base + "/sessions", QByteArray(listing).replace("task=Original", "task=Updated")));
            refresh->trigger();
            QTRY_COMPARE(alpha->session().task, QStringLiteral("Updated"));
            QCOMPARE(window.attentionView(ids[1].slot()).state, State::Waiting);
            QVERIFY(window.attentionTicket(ids[0].slot()) == original);
            alpha->disconnectTerminal(); // Explicit test action on a memory widget.
            QCOMPARE(window.attentionView(ids[0].slot()).state, State::Unknown);
            queued(attentionEvent(original, 2, Kind::Update, Phase::Completed));
            QCOMPARE(window.attentionView(ids[0].slot()).state, State::Unknown);
            QCOMPARE(backend.starts, starts);
            auto *reconnect = alpha->findChild<QPushButton *>(); QVERIFY(reconnect);
            reconnect->click();
            QTRY_COMPARE(backend.starts, starts + 1); // Existing explicit reconnect, never an attention callback.
            const auto reconnected = window.attentionTicket(ids[0].slot());
            QVERIFY(reconnected.valid()); QVERIFY(!(reconnected == original));
            QCOMPARE(window.attentionView(ids[0].slot()).state, State::Unknown);
            queued(attentionEvent(original, 3, Kind::Update, Phase::Completed));
            QCOMPARE(window.attentionView(ids[0].slot()).state, State::Unknown);
            source.send(attentionEvent(reconnected));
            QCOMPARE(window.attentionView(ids[0].slot()).state, State::Working);
            const int afterReconnect = backend.starts;
            auto lost = attentionEvent(waitingTicket, 2, Kind::SourceLost);
            source.send(lost);
            QCOMPARE(window.attentionView(ids[1].slot()).state, State::Unknown);
            QCOMPARE(backend.starts, afterReconnect); QVERIFY(gamma);
            QFile calls(base + "/calls"); QVERIFY(calls.open(QIODevice::ReadOnly));
            for (const QByteArray &line : calls.readAll().split('\n')) {
                QVERIFY(line.isEmpty() || line == "projects/list" || line == "session/list");
            }
        }
        QCOMPARE(source.subscriptions(), 0);
        queued(attentionEvent(original, 4, Kind::Update)); // Window/page lifetime guards.
        {
            MainWindow restarted(options, &backend, &source);
            QTRY_VERIFY(restarted.attentionTicket(ids[0].slot()).valid());
            QVERIFY(!(restarted.attentionTicket(ids[0].slot()) == original));
            QCOMPARE(restarted.attentionView(ids[0].slot()).state, State::Unknown);
            queued(attentionEvent(original, 5, Kind::Update, Phase::Completed));
            QCOMPARE(restarted.attentionView(ids[0].slot()).state, State::Unknown);
        }
        // The same synthetic live listing with the real default source is gray.
        {
            MainWindow realDefault(options, &backend);
            auto *tabs = realDefault.findChild<QTabWidget *>(); QVERIFY(tabs);
            QTRY_COMPARE(tabs->count(), 5);
            for (const auto &id : ids) {
                QCOMPARE(realDefault.attentionView(id.slot()).state, State::Unknown);
                QVERIFY(!realDefault.attentionTicket(id.slot()).valid());
            }
        }
        // A future marker provider may discover its first verified run after
        // the window opens. No session polling or process action is needed.
        FakeAttentionSource markerSource;
        MainWindow markers(options, &backend, &markerSource);
        auto *tabs = markers.findChild<QTabWidget *>(); QVERIFY(tabs);
        QTRY_COMPARE(tabs->count(), 5);
        const int starts = backend.starts, creates = backend.creates;
        markerSource.setBinding(ids[0], attentionRun(true));
        const auto ticket = markers.attentionTicket(ids[0].slot()); QVERIFY(ticket.valid());
        Event marker; marker.ticket = ticket; marker.kind = Kind::WaitingSnapshot;
        marker.sequence = 1; marker.waiting = true;
        markerSource.send(marker);
        QCOMPARE(markers.attentionView(ids[0].slot()).state, State::Waiting);
        marker.kind = Kind::WaitingChanged; marker.sequence = 2; marker.waiting = false;
        markerSource.send(marker);
        QCOMPARE(markers.attentionView(ids[0].slot()).state, State::Unknown);
        QCOMPARE(backend.starts, starts); QCOMPARE(backend.creates, creates);
        for (int i = 1; i < tabs->count(); ++i) {
            if (qobject_cast<SessionPage *>(tabs->widget(i))) {
                QVERIFY(tabs->tabToolTip(i).contains("Codex:"));
            }
        }
    }
    void projectIconSafety_data() {
        QTest::addColumn<QString>("scenario");
        for (const char *name : {"valid", "missing", "corrupt", "jpeg", "transparent", "file-limit", "dimension-limit",
                 "file-symlink-outside", "file-symlink-inside", "root-symlink", "ancestor-symlink", "directory", "fifo", "traversal", "relative"}) {
            QTest::newRow(name) << QString::fromLatin1(name);
        }
    }
    void projectIconSafety() {
        QFETCH(QString, scenario);
        QTemporaryDir fixture(fixtureTemplate());
        QVERIFY(fixture.isValid());
        const QString base = QDir(fixture.path()).canonicalPath();
        QVERIFY(QDir().mkpath(base + "/repos/demo"));
        QString root = base + "/repos/demo";
        const QString path = root + "/.icon.png";
        QImage image(32, 16, QImage::Format_ARGB32);
        image.fill(Qt::red);
        if (scenario == "valid") { QVERIFY(image.save(path, "PNG")); }
        else if (scenario == "corrupt") {
            QFile file(path); QVERIFY(file.open(QIODevice::WriteOnly));
            file.write(QByteArray::fromHex("89504e470d0a1a0a") + "invalid PNG");
        } else if (scenario == "jpeg") { QVERIFY(image.save(path, "BMP")); }
        else if (scenario == "transparent") { image.fill(Qt::transparent); QVERIFY(image.save(path, "PNG")); }
        else if (scenario == "file-limit") {
            QFile file(path); QVERIFY(file.open(QIODevice::WriteOnly)); QVERIFY(file.resize(g2ctabs::maxIconBytes + 1));
        } else if (scenario == "dimension-limit") {
            image = QImage(g2ctabs::maxIconDimension + 1, 1, QImage::Format_ARGB32);
            image.fill(Qt::red); QVERIFY(image.save(path, "PNG"));
        } else if (scenario.startsWith("file-symlink")) {
            const QString target = scenario.endsWith("outside") ? base + "/outside.png" : root + "/inside.png";
            QVERIFY(image.save(target, "PNG"));
            QVERIFY(QFile::link(target, path));
        } else if (scenario == "root-symlink") {
            QVERIFY(image.save(path, "PNG"));
            QVERIFY(QFile::link(root, base + "/repos/link"));
            root = base + "/repos/link";
        } else if (scenario == "ancestor-symlink") {
            QVERIFY(image.save(path, "PNG"));
            QVERIFY(QFile::link(base + "/repos", base + "/alias"));
            root = base + "/alias/demo";
        } else if (scenario == "directory") { QVERIFY(QDir().mkdir(path)); }
        else if (scenario == "fifo") { QCOMPARE(::mkfifo(QFile::encodeName(path).constData(), 0600), 0); }
        else if (scenario == "traversal") { root += "/../demo"; }
        else if (scenario == "relative") { root = "repos/demo"; }
        const QImage loaded = g2ctabs::readProjectIcon(root);
        QCOMPARE(!loaded.isNull(), scenario == "valid");
        if (!loaded.isNull()) { QCOMPARE(loaded, image); }
        QTabWidget tabs;
        const QIcon icon = g2ctabs::tabIcon(loaded, false, &tabs);
        QVERIFY(!icon.pixmap(g2ctabs::decorationSize()).isNull()); // Also verifies fallback in every rejected case.
    }
    void projectRootMetadata() {
        QHash<QString, QString> roots;
        const QByteArray good = "project_dir=/registered\nid=demo name=Display root=not-a-path root=/registered/project with spaces\nid=gpt2codex name=Bridge root=/registered/bridge\n";
        QVERIFY(g2ctabs::parseProjectRoots(good, roots));
        QCOMPARE(roots.value("demo"), QStringLiteral("/registered/project with spaces"));
        for (const QByteArray &bad : {QByteArray(), QByteArray("id=demo name=Demo root=/registered/demo\n"),
                 QByteArray("project_dir=/registered\nid=demo name=Demo root=/outside/demo\n"),
                 QByteArray("project_dir=/registered\nid=demo name=Demo root=/registered/../outside\n"),
                 good + "id=demo name=Other root=/registered/other\n", good + "id=other name=Other root=/registered/bridge\n",
                 good + QByteArray("\0", 1), QByteArray(65537, 'x')}) {
            QVERIFY(!g2ctabs::parseProjectRoots(bad, roots));
            QVERIFY(roots.isEmpty());
        }
    }
    void tabIconGeometryAndFallback() {
        QTabWidget tabs;
        QWidget page;
        tabs.addTab(&page, "Title");
        const int plainHeight = tabs.tabBar()->sizeHint().height();
        for (const QSize &size : {QSize(80, 40), QSize(20, 80)}) {
            QImage image(size, QImage::Format_ARGB32);
            image.fill(Qt::red);
            const QIcon icon = g2ctabs::tabIcon(image, false, &tabs);
            for (qreal dpr : {1.0, 2.0}) {
                const QPixmap pixmap = icon.pixmap(g2ctabs::decorationSize(), dpr, QIcon::Normal, QIcon::Off);
                // Measure project artwork separately from the newly filled
                // attention slot; its aspect ratio must remain unchanged.
                const QRect bounds = opaqueBounds(pixmap.toImage().copy(0, 0, qRound(20 * dpr), qRound(20 * dpr)));
                QVERIFY(!bounds.isEmpty());
                QVERIFY(bounds.width() <= qRound(20 * dpr));
                QVERIFY(bounds.height() <= qRound(20 * dpr));
                QVERIFY(bounds.right() < qRound(20 * dpr));
                QVERIFY(qAbs(double(bounds.width()) / bounds.height() - double(size.width()) / size.height()) < 0.12);
            }
        }
        const QString oldTheme = QIcon::themeName();
        QIcon::setThemeName("g2c-missing-test-theme");
        for (bool dark : {false, true}) {
            QPalette palette = tabs.palette();
            palette.setColor(QPalette::Window, dark ? Qt::black : Qt::white);
            tabs.setPalette(palette);
            for (bool bridge : {false, true}) {
                const QIcon icon = g2ctabs::tabIcon({}, bridge, &tabs);
                QVERIFY(!opaqueBounds(icon.pixmap(g2ctabs::decorationSize()).toImage()).isEmpty());
                tabs.setIconSize(g2ctabs::decorationSize());
                tabs.setTabIcon(0, icon);
            }
        }
        QIcon::setThemeName(oldTheme);
        QVERIFY(tabs.tabBar()->sizeHint().height() <= plainHeight + 8);
    }
    void tabPresentationPreservesSessionViews() {
        QTemporaryDir fixture(fixtureTemplate());
        QVERIFY(fixture.isValid());
        const QString base = QDir(fixture.path()).canonicalPath();
        for (const QString &name : {QStringLiteral("demo"), QStringLiteral("bridge")}) {
            QVERIFY(QDir().mkdir(base + "/" + name));
            QImage image(20, 20, QImage::Format_ARGB32);
            image.fill(name == "demo" ? Qt::red : Qt::blue);
            QVERIFY(image.save(base + "/" + name + "/.icon.png", "PNG"));
        }
        MemoryBackend backend;
        BackendRegistry registry(backend);
        QTabWidget tabs;
        Session screen = live;
        screen.id = "demo"; screen.screenName = "g2c-demo"; screen.sessionId = "123.g2c-demo"; screen.windowTitle = "g2c-demo";
        Session tmux = screen;
        tmux.multiplexer = "tmux"; tmux.sessionId = "321.$0.123456";
        auto *first = new SessionPage({}, registry, "konsole", screen, &tabs);
        auto *second = new SessionPage({}, registry, "konsole", tmux, &tabs);
        tabs.addTab(first, displayName(screen)); tabs.addTab(second, displayName(tmux));
        const auto terminal = backend.last;
        g2ctabs::Presentation presentation;
        presentation.setRoots({{"demo", base + "/demo"}, {"gpt2codex", base + "/bridge"}});
        for (int i = 0; i < 3; ++i) {
            presentation.apply(&tabs, first, screen.id, screen.multiplexer, displayName(screen), "<img src=/private/file>");
            presentation.apply(&tabs, second, tmux.id, tmux.multiplexer, displayName(tmux), tmux.task);
            tabs.setCurrentWidget(i % 2 ? first : second);
            first->update(screen); second->update(tmux);
        }
        QCOMPARE(backend.starts, 2);
        QVERIFY(terminal);
        QVERIFY(second->containsTerminal(terminal));
        QCOMPARE(tabs.tabText(0), displayName(screen));
        QCOMPARE(tabs.tabText(1), displayName(tmux));
        QVERIFY(tabs.tabToolTip(0).contains(base + "/demo"));
        QVERIFY(tabs.tabToolTip(0).contains("[screen]"));
        QVERIFY(tabs.tabToolTip(1).contains("[tmux]"));
        QVERIFY(!tabs.tabToolTip(0).contains("<img"));
        QCOMPARE(tabs.tabIcon(0).pixmap(g2ctabs::decorationSize()).toImage().pixelColor(10, 10), QColor(Qt::red));
        backend.last->finish();
        second->activateTerminal(); second->update(tmux);
        presentation.apply(&tabs, second, tmux.id, tmux.multiplexer, displayName(tmux), tmux.task);
        QCOMPARE(backend.starts, 3);
        QCOMPARE(tabs.tabIcon(1).pixmap(g2ctabs::decorationSize()).toImage().pixelColor(10, 10), QColor(Qt::red));
        QVERIFY(!backend.arguments.contains("start"));
        // A registry lookup failure must drop a previously loaded custom image.
        presentation.setRoots({});
        presentation.apply(&tabs, first, screen.id, screen.multiplexer, displayName(screen), screen.task);
        QVERIFY(!tabs.tabToolTip(0).contains(base));
        QVERIFY(tabs.tabToolTip(0).contains("cesta není ověřena"));
    }
    void windowRefreshKeepsProjectIconsAndActualMultiplexers() {
        QTemporaryDir fixture(fixtureTemplate());
        QVERIFY(fixture.isValid());
        const QString base = QDir(fixture.path()).canonicalPath();
        for (const QString &project : {QStringLiteral("demo"), QStringLiteral("other"), QStringLiteral("bridge")}) {
            QVERIFY(QDir().mkpath(base + "/repos/" + project));
            QImage image(20, 20, QImage::Format_ARGB32);
            image.fill(project == "demo" ? Qt::red : project == "other" ? Qt::green : Qt::blue);
            QVERIFY(image.save(base + "/repos/" + project + "/.icon.png", "PNG"));
        }
        auto write = [](const QString &path, const QByteArray &bytes) {
            QFile file(path);
            return file.open(QIODevice::WriteOnly | QIODevice::Truncate) && file.write(bytes) == bytes.size();
        };
        const QByteArray roots = ("project_dir=" + base + "/repos\n"
            + "id=demo name=Demo root=" + base + "/repos/demo\n"
            + "id=other name=Other root=" + base + "/repos/other\n"
            + "id=gpt2codex name=Bridge root=" + base + "/repos/bridge\n").toUtf8();
        QVERIFY(write(base + "/roots", roots));
        const QByteArray listing =
            "multiplexer=screen generation=- project_id=bridge screen_name=g2c-bridge state=not_registered session_id=- task=Bridge\n"
            "multiplexer=screen generation=123 project_id=demo screen_name=g2c-demo state=detached session_id=123.g2c-demo program=running exit_code=- exit_signal=- exited_at=- window=0 window_title=g2c-demo task=Original\n"
            "multiplexer=tmux generation=456 project_id=demo screen_name=g2c-demo state=detached session_id=321.$0.123456 program=running exit_code=- exit_signal=- exited_at=- window=- window_title=- task=Original\n"
            "multiplexer=screen generation=789 project_id=other screen_name=g2c-other state=detached session_id=789.g2c-other program=running exit_code=- exit_signal=- exited_at=- window=0 window_title=g2c-other task=Other\n";
        QVERIFY(write(base + "/sessions", listing));
        const QString cli = base + "/fixture-cli";
        QVERIFY(write(cli, "#!/bin/sh\nbase=${0%/*}\nprintf '%s/%s\\n' \"$1\" \"$2\" >> \"$base/calls\"\ncase \"$1/$2\" in\nprojects/list) /bin/cat \"$base/roots\";;\nsession/list) /bin/cat \"$base/sessions\";;\nsession/prefer) printf 'project_id=demo preferred=tmux\\n';;\n*) exit 89;;\nesac\n"));
        QVERIFY(QFile::setPermissions(cli, QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner));
        QJsonObject queueRoots;
        for (const auto &project : {QStringLiteral("gpt2codex"), QStringLiteral("demo"), QStringLiteral("other")}) {
            queueRoots.insert(project, base + "/repos/" + project);
        }
        const QString queueConfig = base + "/queue.json";
        QVERIFY(write(queueConfig, QJsonDocument(QJsonObject{{"version", 1}, {"project_roots", queueRoots}}).toJson(QJsonDocument::Compact)));
        Options options; options.bridgeCli = cli; options.config = base + "/fixture-config"; options.queueConfig = queueConfig;
        MemoryBackend backend;
        MainWindow window(options, &backend);
        auto *tabs = window.findChild<QTabWidget *>();
        QVERIFY(tabs);
        QTRY_COMPARE(tabs->count(), 5);
        QTRY_COMPARE(backend.starts, 3); // Active fixtures use the in-memory terminal; never invoke real attach.
        QCOMPARE(backend.creates, 3);
        QTRY_VERIFY(tabs->tabToolTip(1).contains(base + "/repos/demo"));
        auto *screen = qobject_cast<SessionPage *>(tabs->widget(1));
        auto *tmux = qobject_cast<SessionPage *>(tabs->widget(2));
        auto *other = qobject_cast<SessionPage *>(tabs->widget(3));
        QVERIFY(screen && tmux && other);
        const QString originalId = screen->session().sessionId;
        QCOMPARE(tabs->tabText(0), QString("Queue Worker"));
        QVERIFY(!tabs->tabBar()->tabButton(0, QTabBar::RightSide));
        auto checkPage = [&](SessionPage *page, const QString &root, const QColor &color) {
            const int index = tabs->indexOf(page);
            return index >= 0 && tabs->tabToolTip(index).contains(root)
                && tabs->tabToolTip(index).contains("[" + page->session().multiplexer + "]")
                && tabs->tabIcon(index).pixmap(g2ctabs::decorationSize()).toImage().pixelColor(10, 10) == color;
        };
        auto action = [&](const QString &text) -> QAction * {
            for (auto *item : window.findChildren<QAction *>()) { if (item->text() == text) { return item; } }
            return nullptr;
        };
        QAction *refresh = action("Obnovit seznam");
        QVERIFY(refresh);
        QVERIFY(checkPage(screen, base + "/repos/demo", Qt::red));
        QVERIFY(checkPage(tmux, base + "/repos/demo", Qt::red));
        QVERIFY(checkPage(other, base + "/repos/other", Qt::green));
        tabs->tabBar()->moveTab(3, 1);
        tabs->setCurrentWidget(tmux);
        // A real preference save uses only its existing local CLI operation;
        // actual session multiplexer/identity and tab artwork remain unchanged.
        ProjectMultiplexerSettings settings(options, nullptr, false);
        QVERIFY(settings.loadProjects("project_id=demo preferred=screen\n"));
        settings.setAvailable(true, true);
        settings.findChild<QComboBox *>("projectMultiplexer")->setCurrentIndex(1);
        settings.findChild<QPushButton *>("saveProjectMultiplexer")->click();
        QTRY_VERIFY(settings.findChild<QComboBox *>("projectMultiplexer")->isEnabled());
        QVERIFY(write(base + "/sessions", QByteArray(listing).replace("task=Original", "task=Refreshed")));
        refresh->trigger();
        QTRY_COMPARE(screen->session().task, QStringLiteral("Refreshed"));
        QCOMPARE(screen->session().sessionId, originalId);
        QCOMPARE(screen->session().multiplexer, QStringLiteral("screen"));
        QCOMPARE(backend.starts, 3);
        QVERIFY(checkPage(screen, base + "/repos/demo", Qt::red));
        QVERIFY(checkPage(tmux, base + "/repos/demo", Qt::red));
        QVERIFY(checkPage(other, base + "/repos/other", Qt::green));
        // Closing/restoring a tab must rederive the icon from its project, not index.
        const int otherIndex = tabs->indexOf(other);
        tabs->tabCloseRequested(otherIndex);
        QTRY_COMPARE(tabs->count(), 4);
        QAction *restore = action("Zobrazit skryté záložky");
        QVERIFY(restore); restore->trigger();
        QTRY_COMPARE(tabs->count(), 5);
        auto *restored = qobject_cast<SessionPage *>(tabs->widget(3));
        QVERIFY(restored);
        QVERIFY(checkPage(restored, base + "/repos/other", Qt::green));
        QVERIFY(write(base + "/roots", "invalid registry\n"));
        refresh->trigger();
        QTRY_VERIFY(tabs->tabToolTip(tabs->indexOf(screen)).contains("cesta není ověřena"));
        QFile calls(base + "/calls");
        QVERIFY(calls.open(QIODevice::ReadOnly));
        const QByteArray called = calls.readAll();
        QVERIFY(called.contains("session/prefer\n"));
        for (const QByteArray &line : called.split('\n')) {
            QVERIFY(line.isEmpty() || line == "projects/list" || line == "session/list" || line == "session/prefer");
        }
    }
    void screenAnchorIsNotACodexAndManualWindowIsExact() {
        QList<Session> parsed; QString error;
        const QByteArray bridge = "multiplexer=screen generation=- project_id=bridge screen_name=g2c-bridge state=not_registered session_id=- task=Bridge\n";
        const QByteArray anchor = "multiplexer=screen generation=123 project_id=demo screen_name=g2c-demo state=attached session_id=123.g2c-demo program=anchor exit_code=- exit_signal=- exited_at=- window=0 window_title=g2c-anchor task=Codex\n" + bridge;
        QVERIFY(parseSessions(anchor, parsed, error));
        QVERIFY(!parsed[0].hasCodexView());
        QCOMPARE(parsed[0].windowTitle, QStringLiteral("g2c-anchor"));
        parsed.clear();
        const QByteArray manual = "multiplexer=screen generation=123 project_id=demo screen_name=g2c-demo state=attached session_id=123.g2c-demo program=running exit_code=- exit_signal=- exited_at=- window=2 window_title=g2c-demo task=Codex\n" + bridge;
        QVERIFY(parseSessions(manual, parsed, error));
        QVERIFY(parsed[0].hasCodexView());
        QCOMPARE(attachArguments({}, parsed[0]), QStringList({"session", "attach", "--multiplexer", "screen",
            "--expect-session-id", "123.g2c-demo", "--expect-process-start-ticks", "123",
            "--id", "demo", "--window", "2"}));
        parsed.clear();
        QVERIFY(!parseSessions(QByteArray(manual).replace("window_title=g2c-demo", "window_title=g2c-anchor"), parsed, error));
    }

    void exitedProgramKeepsVerifiedTerminal() {
        const QByteArray listing = "multiplexer=tmux generation=456 project_id=bridge screen_name=g2c-bridge state=detached session_id=321.$0.123456 program=exited exit_code=23 exit_signal=- exited_at=123457 task=Bridge\n";
        QList<Session> parsed;
        QString error;
        QVERIFY(parseSessions(listing, parsed, error));
        QCOMPARE(parsed[0].programState, QStringLiteral("exited"));
        MemoryBackend backend;
        BackendRegistry registry(backend);
        SessionPage page({}, registry, "konsole", parsed[0]);
        QCOMPARE(backend.starts, 1);
        QVERIFY(page.findChild<QLabel *>("sessionStatus")->text().contains("ukončen (exit 23"));
        for (int i=0; i<5; ++i) { page.update(parsed[0]); page.activateTerminal(); }
        QCOMPARE(backend.starts, 1);
        backend.last->finish();
        page.activateTerminal();
        page.update(parsed[0]);
        QCOMPARE(backend.starts, 2); // Reconnect only; no start/respawn arguments.
        QVERIFY(!backend.arguments.contains("start"));
        parsed.clear();
        QVERIFY(!parseSessions(QByteArray(listing).replace("program=exited", "program=running"), parsed, error));
        parsed.clear();
        QVERIFY(!parseSessions(QByteArray(listing).replace("exit_code=23", "exit_code=999"), parsed, error));
    }
    void bridgeStartup() {
        MemoryBackend backend;
        BackendRegistry registry(backend);
        SessionPage page({}, registry, "konsole", missing);
        QCOMPARE(backend.starts, 0);
        page.update(live);
        QCOMPARE(backend.starts, 1);
        QCOMPARE(backend.arguments, QStringList({"session", "attach", "--multiplexer", "screen",
            "--expect-session-id", "123.g2c-bridge", "--bridge", "--window", "0"}));
        for (int i = 0; i < 5; ++i) { page.update(live); page.activateTerminal(); }
        QCOMPARE(backend.starts, 1);
    }
    void missingBridgeNeverStarts() {
        MemoryBackend backend;
        BackendRegistry registry(backend);
        SessionPage page({}, registry, "konsole", missing);
        page.update(missing);
        page.activateTerminal();
        QCOMPARE(backend.starts, 0);
    }
    void bridgeStartupChoosesExistingMultiplexer() {
        Session tmux = live;
        tmux.multiplexer = "tmux";
        tmux.sessionId = "321.$0.123456";
        QCOMPARE(startupBridgeKey({missing, tmux}), QStringLiteral("tmux:bridge"));
        QCOMPARE(startupBridgeKey({live, tmux}), QStringLiteral("screen:bridge"));
        QCOMPARE(startupBridgeKey({missing}), QStringLiteral("screen:bridge"));
        MemoryBackend backend;
        BackendRegistry registry(backend);
        SessionPage page({}, registry, "konsole", tmux);
        QCOMPARE(backend.starts, 1);
        QVERIFY(backend.arguments.contains("tmux"));
    }
    void activationReconnectIsBoundedAndVerified() {
        MemoryBackend backend;
        BackendRegistry registry(backend);
        SessionPage page({}, registry, "konsole", live);
        int queries = 0;
        page.setRefreshRequested([&] { ++queries; });
        backend.last->finish();
        for (int i = 0; i < 5; ++i) { page.update(live); }
        QCOMPARE(backend.starts, 1);
        page.activateTerminal();
        page.activateTerminal();
        QCOMPARE(queries, 1);
        QCOMPARE(backend.starts, 1); // No attach before a new validated listing.
        page.update(live);
        QCOMPARE(backend.starts, 2);
        backend.last->finish();
        for (int i = 0; i < 5; ++i) { page.update(live); }
        QCOMPARE(backend.starts, 2); // Failed reconnect cannot loop.
    }
    void replacementBlocksAutomaticReconnect() {
        MemoryBackend backend;
        BackendRegistry registry(backend);
        SessionPage page({}, registry, "konsole", live);
        backend.last->finish();
        page.activateTerminal();
        Session replacement = live;
        replacement.sessionId = "456.g2c-bridge";
        page.update(replacement);
        page.activateTerminal();
        page.update(replacement);
        QCOMPARE(backend.starts, 1);
        page.findChild<QPushButton *>()->click();
        page.update(replacement);
        QCOMPARE(backend.starts, 2);
    }
    void intentionalDetachSurvivesActivationAndBackendChange() {
        MemoryBackend backend;
        BackendRegistry registry(backend);
        SessionPage page({}, registry, "konsole", live);
        page.disconnectTerminal();
        page.activateTerminal();
        page.update(live);
        page.prepareBackendChange("konsole");
        page.update(live);
        QCOMPARE(backend.starts, 1);
        page.findChild<QPushButton *>()->click();
        page.update(live);
        QCOMPARE(backend.starts, 2);
    }
    void unavailableIdentityRequiresManualReconnect() {
        MemoryBackend backend;
        BackendRegistry registry(backend);
        SessionPage page({}, registry, "konsole", live);
        Session unavailable = live;
        unavailable.state = "unconfirmed";
        page.update(unavailable);
        page.update(live);
        page.activateTerminal();
        page.update(live);
        QCOMPARE(backend.starts, 1);
    }
    void backendChangeReattachesOnlyConfirmedLiveViews() {
        MemoryBackend backend;
        BackendRegistry registry(backend);
        SessionPage page({}, registry, "konsole", live);
        page.prepareBackendChange("konsole");
        QCOMPARE(backend.starts, 1);
        page.update(live);
        QCOMPARE(backend.starts, 2);
        page.prepareBackendChange("konsole");
        Session replacement = live;
        replacement.sessionId = "456.g2c-bridge";
        page.update(replacement);
        QCOMPARE(backend.starts, 2);
    }
    void reusedPidBlocksReconnect() {
        MemoryBackend backend;
        BackendRegistry registry(backend);
        Session original = live;
        original.generation = "12345";
        SessionPage page({}, registry, "konsole", original);
        QVERIFY(backend.arguments.contains("--expect-process-start-ticks"));
        backend.last->finish();
        page.activateTerminal();
        Session reused = original;
        reused.generation = "23456";
        page.update(reused);
        page.activateTerminal();
        page.update(reused);
        QCOMPARE(backend.starts, 1);
    }
    void screenDetachIsRespected() {
        MemoryBackend backend;
        BackendRegistry registry(backend);
        SessionPage page({}, registry, "konsole", live);
        QTest::keyClick(backend.last, Qt::Key_A, Qt::ControlModifier);
        QTest::keyClick(backend.last, Qt::Key_D);
        backend.last->finish();
        page.activateTerminal();
        page.update(live);
        QCOMPARE(backend.starts, 1);
    }
    void tmuxReconnectAndDetach() {
        MemoryBackend backend;
        BackendRegistry registry(backend);
        Session tmux = live;
        tmux.multiplexer = "tmux";
        tmux.sessionId = "321.$0.123456";
        SessionPage page({}, registry, "konsole", tmux);
        QVERIFY(backend.arguments.contains("tmux"));
        QVERIFY(backend.arguments.contains(tmux.sessionId));
        backend.last->finish();
        page.activateTerminal();
        page.update(tmux);
        QCOMPARE(backend.starts, 2);
        QTest::keyClick(backend.last, Qt::Key_B, Qt::ControlModifier);
        QTest::keyClick(backend.last, Qt::Key_D);
        backend.last->finish();
        page.activateTerminal();
        page.update(tmux);
        QCOMPARE(backend.starts, 2);
        QVERIFY(!sameLiveSessionIdentity(live, tmux));
    }
    void multiplexersHaveDistinctSlots() {
        const QByteArray raw =
            "multiplexer=screen generation=123 project_id=bridge screen_name=g2c-bridge state=detached session_id=123.g2c-bridge task=Bridge\n"
            "multiplexer=tmux generation=456 project_id=bridge screen_name=g2c-bridge state=attached session_id=321.$0.123456 task=Bridge\n";
        QList<Session> parsed;
        QString error;
        QVERIFY(parseSessions(raw, parsed, error));
        QCOMPARE(parsed.size(), 2);
        QVERIFY(parsed[0].key() != parsed[1].key());
        QVERIFY(displayName(parsed[0]).contains("screen"));
        QVERIFY(displayName(parsed[1]).contains("tmux"));
        parsed.clear();
        QVERIFY(!parseSessions(raw + raw, parsed, error));
        parsed.clear();
        QByteArray swapped = raw;
        swapped.replace("321.$0.123456", "321.g2c-bridge");
        QVERIFY(!parseSessions(swapped, parsed, error));
    }
    void projectPreferenceAvailability() {
        ProjectMultiplexerSettings settings({}, nullptr, false);
        QVERIFY(settings.loadProjects("project_id=demo preferred=screen\nproject_id=other preferred=tmux\n"));
        settings.setAvailable(true, false);
        auto *projects = settings.findChild<QComboBox *>("multiplexerProject");
        auto *mux = settings.findChild<QComboBox *>("projectMultiplexer");
        auto *save = settings.findChild<QPushButton *>("saveProjectMultiplexer");
        QCOMPARE(mux->currentData().toString(), QStringLiteral("screen"));
        auto *model = qobject_cast<QStandardItemModel *>(mux->model());
        QVERIFY(model->item(0)->isEnabled());
        QVERIFY(!model->item(1)->isEnabled());
        QVERIFY(!model->item(1)->toolTip().isEmpty());
        projects->setCurrentIndex(projects->findData("other"));
        QCOMPARE(mux->currentData().toString(), QStringLiteral("tmux")); // Preserve an unavailable stored choice.
        QVERIFY(!save->isEnabled());
        mux->setCurrentIndex(0);
        QVERIFY(save->isEnabled());
        settings.setAvailable(true, true);
        QVERIFY(model->item(1)->isEnabled());
        QVERIFY(!settings.loadProjects("project_id=demo preferred=screen\nproject_id=demo preferred=tmux\n"));
    }
    void inactiveTerminalsArePreserved() {
        MemoryBackend backend;
        BackendRegistry registry(backend);
        SessionPage first({}, registry, "konsole", live);
        QPointer<MemoryTerminal> original = backend.last;
        SessionPage second({}, registry, "konsole", live);
        first.hide();
        second.activateTerminal();
        first.update(live);
        QVERIFY(original);
        QVERIFY(first.containsTerminal(original));
        QCOMPARE(backend.starts, 2);
    }
    void pasteOverridesConflictingActions_data() {
        QTest::addColumn<int>("key");
        QTest::addColumn<Qt::KeyboardModifiers>("modifiers");
        QTest::newRow("ctrl-shift-v") << int(Qt::Key_V) << Qt::KeyboardModifiers(Qt::ControlModifier | Qt::ShiftModifier);
        QTest::newRow("shift-insert") << int(Qt::Key_Insert) << Qt::KeyboardModifiers(Qt::ShiftModifier);
    }
    void pasteOverridesConflictingActions() {
        QFETCH(int, key);
        QFETCH(Qt::KeyboardModifiers, modifiers);
        QWidget window;
        MemoryTerminal terminal(&window);
        terminal.setFocusPolicy(Qt::StrongFocus);
        QWidget settings(&window);
        QAction menuPaste(&window), kpartPaste(&window);
        const QKeySequence shortcut(modifiers.toInt() | key);
        menuPaste.setShortcut(shortcut);
        kpartPaste.setShortcut(shortcut);
        window.addAction(&menuPaste);
        window.addAction(&kpartPaste);
        int kpartCalls = 0;
        connect(&menuPaste, &QAction::triggered, &terminal, [&] { terminal.pasteClipboardText(); });
        connect(&kpartPaste, &QAction::triggered, &terminal, [&] { ++kpartCalls; });
        PasteShortcutFilter router(&window,
            [&](QWidget *target) { return target == &terminal; }, [&] { menuPaste.trigger(); });
        window.show();
        window.activateWindow();
        terminal.setFocus();
        QCoreApplication::processEvents();
        QTest::keyClick(&terminal, Qt::Key(key), modifiers);
        QCOMPARE(terminal.pastes, 1);
        QCOMPARE(kpartCalls, 0);
        menuPaste.trigger();
        QCOMPARE(terminal.pastes, 2); // Menu and keys reach exactly the same operation.
        QKeyEvent override(QEvent::ShortcutOverride, key, modifiers);
        override.ignore();
        QCoreApplication::sendEvent(&settings, &override);
        QVERIFY(!override.isAccepted());
        QTest::keyClick(&terminal, Qt::Key_V, Qt::ControlModifier);
        QCOMPARE(terminal.pastes, 2); // Ctrl+V remains available to Codex.
    }
};

} // namespace
QTEST_MAIN(GuiLifecycleTests)
