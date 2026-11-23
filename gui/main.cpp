// Local Qt6 view of bridge-owned screen/tmux sessions.
// The Rust CLI is the authority for session identity and lifecycle.
// No screen start, screen -X, Codex prompt submission, or MCP write operation.
#include "backend_interface.h"
#include "terminal_theme.h"
#include "paste_shortcut_filter.h"
#include "attention_source.h"
#include "marker_attention_source.h"

#include <QAction>
#include <QDialog>
#include <QJsonDocument>
#include <QJsonArray>
#include <QJsonObject>
#include <QApplication>
#include "queue_worker_monitor.h"
#include <QClipboard>
#include <QCheckBox>
#include <QCloseEvent>
#include <QColor>
#include <QCoreApplication>
#include <QGuiApplication>
#include <QKeySequence>
#include <QMetaObject>
#include <QMimeData>
#include <QDir>
#include <QEvent>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QFontDatabase>
#include <QHash>
#include <QImageReader>
#include <QImage>
#include <QIcon>
#include <QLabel>
#include <QPlainTextEdit>
#include <QMainWindow>
#include <QMenu>
#include <QComboBox>
#include <QFormLayout>
#include <QSpinBox>
#include <QPluginLoader>
#include <QLibrary>
#include <QMessageBox>
#include <QMenuBar>
#include <QPainter>
#include <QPalette>
#include <QProcess>
#include <QPointer>
#include <QPixmap>
#include <QPushButton>
#include <QRegularExpression>
#include <QSet>
#include <QSettings>
#include <QStandardPaths>
#include <QStandardItemModel>
#include <QSignalBlocker>
#include <QVariant>
#include <QStatusBar>
#include <QGroupBox>
#include <QListWidget>
#include <QStackedWidget>
#include <QSplitter>
#include <QHBoxLayout>
#include <QFont>
#include <QFrame>
#include <QStyle>
#include <QSystemTrayIcon>
#include <QTabBar>
#include <QTabWidget>
#include <QTimer>
#include <QTemporaryDir>
#include <QVBoxLayout>

#include <cstdio>
#include <functional>
#include <memory>
#include <utility>
#include <vector>
#include <signal.h>
#include <unistd.h>

#include "project_tab_presentation.h"

namespace {

using namespace g2ctheme;

// Only bridge session-list queries get a private process group. Never target
// the detached project screen servers (which belong to different groups).
void killSessionListQuery(QProcess *process) {
    if (!process) { return; }
    const auto group = process->property("_g2cListGroup").toLongLong();
    if (group > 0) {
        (void)::kill(-static_cast<pid_t>(group), SIGKILL);
    } else if (process->state() != QProcess::NotRunning) {
        process->kill();
    }
}

struct Options {
    QString bridgeCli;
    QString config;
    QString queueHelper;
    QString queueConfig;
    QString queueRepoRoot;    // Mutable worker checkout, never the installed script directory.
    QString queueRootError;
    bool queueOnly = false;
    bool selfTest = false;
    bool themeProbe = false;
    bool settingsProbe = false;
    bool backendProbe = false;
};

// Locate both Queue entry points together, preferring the installed
// directory over the development checkout.
// Discover the *pair*: a lone screen helper cannot display or approve GitHub
// Issues. In particular gpt2codex-github-queue.py is NOT --queue-helper.
QString localQueueHelper() {
    const QString app = QCoreApplication::applicationDirPath();
    const QStringList roots{app, app + "/../../", app + "/../lib/gpt2codex",
        app + "/../share/gpt2codex", QString::fromUtf8(G2C_GUI_SOURCE_ROOT)};
    for (const auto &root : roots) {
        const QFileInfo screen(QDir(root).filePath(QStringLiteral("gpt2codex_queue_screen.py")));
        const QFileInfo queue(QDir(root).filePath(QStringLiteral("gpt2codex-github-queue.py")));
        if (screen.isFile() && screen.isReadable() && queue.isFile() && queue.isReadable()
            && QFileInfo(screen.canonicalFilePath()).absolutePath()
                == QFileInfo(queue.canonicalFilePath()).absolutePath()) {
            return screen.canonicalFilePath();
        }
    }
    return {};
}

// Same XDG/HOME rule as the CLI. Resolve once and pass this exact config to
// discovery, settings and attach, so the displayed diagnostic is reproducible.
QString defaultBridgeConfig() {
    const QString xdg = qEnvironmentVariable("XDG_CONFIG_HOME");
    return QDir(QFileInfo(xdg).isAbsolute() ? xdg : QDir::homePath() + "/.config")
        .filePath("chatgpt2codex/config.toml");
}

QString defaultQueueConfig() {
    // Same default as gpt2codex-github-queue.py, deliberately independent of XDG.
    return QDir::homePath() + "/.config/gpt2codex/github-queue/config.json";
}

// A packaged helper lives in /usr/share/gpt2codex, while the worker's actual
// state/log paths are relative to its existing writable project checkout.
// Never let an installed helper silently start using /usr/share as a store.
QString queueWorkingRoot(const QString &configPath, const QString &screenHelper) {
    QStringList candidates;
    QFile file(configPath);
    if (file.open(QIODevice::ReadOnly) && file.size() <= 64 * 1024) {
        const auto config = QJsonDocument::fromJson(file.readAll()).object();
        if (config.value(QStringLiteral("version")).toInt(-1) == 1) {
            const auto projects = config.value(QStringLiteral("project_roots")).toObject();
            const auto checkout = projects.value(QStringLiteral("gpt2codex")).toString();
            if (!checkout.isEmpty()) { candidates << checkout; }
        }
    }
    if (!screenHelper.isEmpty()) { candidates << QFileInfo(screenHelper).absolutePath(); }
    for (const QString &candidate : candidates) {
        const QFileInfo root(candidate);
        if (!root.isAbsolute() || !root.isDir()) { continue; }
        const QString canonical = root.canonicalFilePath();
        if (!canonical.isEmpty() && QFileInfo(canonical + QStringLiteral("/.tmp")).isDir()
            && QFileInfo(canonical + QStringLiteral("/.log")).isDir()) {
            return canonical;
        }
    }
    return {};
}

bool queueAllowedProjects(const QString &path, QSet<QString> &ids, QString &error) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly) || file.size() > 1024 * 1024) {
        error = QStringLiteral("GitHub Queue allowlist nelze načíst: %1").arg(path);
        return false;
    }
    const auto doc = QJsonDocument::fromJson(file.readAll());
    const auto roots = doc.object().value("project_roots");
    if (!doc.isObject() || doc.object().value("version").toInt(-1) != 1
        || !roots.isObject() || roots.toObject().isEmpty()) {
        error = QStringLiteral("GitHub Queue konfigurace nemá platný project_roots: %1").arg(path);
        return false;
    }
    QSet<QString> fresh;
    const auto object = roots.toObject();
    for (auto it = object.begin(); it != object.end(); ++it) {
        if (!QRegularExpression("^[a-zA-Z0-9_-]{1,64}$").match(it.key()).hasMatch()
            || !it.value().isString() || !QFileInfo(it.value().toString()).isAbsolute()) {
            error = QStringLiteral("GitHub Queue project_roots je neplatný: %1").arg(path);
            return false;
        }
        fresh.insert(it.key());
    }
    ids = fresh;
    return true;
}

bool parseOptions(const QStringList &arguments, Options &options, QString &error) {
    options.bridgeCli = QCoreApplication::applicationDirPath() + QStringLiteral("/gpt2codex");
    if (!QFileInfo(options.bridgeCli).isExecutable()) {
        options.bridgeCli = QStandardPaths::findExecutable(QStringLiteral("gpt2codex"));
        if (options.bridgeCli.isEmpty()) {
            for (const QString &build : {QStringLiteral("release"), QStringLiteral("debug")}) {
                const QString candidate = QString::fromUtf8(G2C_GUI_SOURCE_ROOT)
                    + QStringLiteral("/target/") + build + QStringLiteral("/gpt2codex");
                if (QFileInfo(candidate).isFile() && QFileInfo(candidate).isExecutable()) {
                    options.bridgeCli = candidate; break;
                }
            }
        }
    }
    bool sawBridgeCli = false;
    bool sawConfig = false;
    for (int i = 1; i < arguments.size(); ++i) {
        const QString arg = arguments.at(i);
        if (arg == QStringLiteral("--queue-only") && !options.queueOnly) {
            options.queueOnly = true;
        } else if (arg == QStringLiteral("--self-test") && !options.selfTest && !options.themeProbe && !options.settingsProbe && !options.backendProbe) {
            options.selfTest = true;
        } else if (arg == QStringLiteral("--theme-probe") && !options.themeProbe && !options.selfTest && !options.settingsProbe && !options.backendProbe) {
            options.themeProbe = true;
        } else if (arg == QStringLiteral("--settings-probe") && !options.settingsProbe && !options.selfTest && !options.themeProbe && !options.backendProbe) {
            options.settingsProbe = true;
        } else if (arg == QStringLiteral("--backend-probe") && !options.backendProbe && !options.selfTest && !options.themeProbe && !options.settingsProbe) {
            options.backendProbe = true;
        } else if (arg == QStringLiteral("--bridge-cli") && !sawBridgeCli && i + 1 < arguments.size()) {
            options.bridgeCli = arguments.at(++i);
            sawBridgeCli = true;
        } else if (arg == QStringLiteral("--queue-helper") && options.queueHelper.isEmpty() && i + 1 < arguments.size()) {
            options.queueHelper = arguments.at(++i);
        } else if (arg == QStringLiteral("--queue-config") && options.queueConfig.isEmpty() && i + 1 < arguments.size()) {
            options.queueConfig = arguments.at(++i);
        } else if (arg == QStringLiteral("--config") && !sawConfig && i + 1 < arguments.size()) {
            options.config = arguments.at(++i);
            sawConfig = true;
        } else {
            error = QStringLiteral("Neplatný nebo opakovaný parametr: %1").arg(arg);
            return false;
        }
    }
    const QFileInfo cli(options.bridgeCli);
    if ((!cli.isAbsolute() || !cli.isFile() || !cli.isExecutable()) && (!options.queueOnly || sawBridgeCli)) {
        error = QStringLiteral("Bridge CLI musí být existující spustitelný absolutní soubor: %1").arg(options.bridgeCli);
        return false;
    }
    options.bridgeCli = cli.canonicalFilePath();
    if (!options.config.isEmpty() && !QFileInfo(options.config).isAbsolute()) {
        error = QStringLiteral("--config vyžaduje absolutní cestu");
        return false;
    }
    if (options.config.isEmpty()) { options.config = defaultBridgeConfig(); }
    if (options.queueConfig.isEmpty()) { options.queueConfig = defaultQueueConfig(); }
    if (!QFileInfo(options.queueConfig).isAbsolute()) {
        error = QStringLiteral("--queue-config vyžaduje absolutní cestu");
        return false;
    }
    if (options.queueHelper.isEmpty()) {
        options.queueHelper = localQueueHelper();
    }
    if (!options.queueHelper.isEmpty()) {
        const QFileInfo helper(options.queueHelper);
        const QFileInfo queue(helper.dir().filePath(QStringLiteral("gpt2codex-github-queue.py")));
        if (!helper.isAbsolute() || !helper.isFile() || !helper.isReadable()
            || helper.fileName() != QStringLiteral("gpt2codex_queue_screen.py")
            || !queue.isFile() || !queue.isReadable()
            || QFileInfo(helper.canonicalFilePath()).absolutePath()
                != QFileInfo(queue.canonicalFilePath()).absolutePath()) {
            error = QStringLiteral("--queue-helper musí být gpt2codex_queue_screen.py "
                "se sourozeneckým gpt2codex-github-queue.py (nikoli samotný GitHub CLI)");
            return false;
        }
        options.queueHelper = helper.canonicalFilePath();
        options.queueRepoRoot = queueWorkingRoot(options.queueConfig, options.queueHelper);
        if (options.queueRepoRoot.isEmpty()) {
            options.queueRootError = QStringLiteral("GitHub Queue: nelze určit pracovní adresář workeru. "
                "V %1 ověř project_roots.gpt2codex a existující .tmp/.log.")
                .arg(options.queueConfig);
        }
    }
    return true;
}

// Follow Qt's effective platform palette. KDE/Plasma supplies the UI style and
// palette through its Qt platform integration; do not override either here.
// QTermWidget uses an independent terminal palette, so select an explicit theme.
constexpr int defaultMaxTabLabelChars = 32;
constexpr int minTabLabelChars = 12;
constexpr int maxTabLabelChars = 128;

QSettings guiSettings() {
    return QSettings(QSettings::IniFormat, QSettings::UserScope,
                     QStringLiteral("gpt2codex"), QStringLiteral("gpt2codex-gui"));
}

int readMaxTabLabelChars() {
    QSettings settings = guiSettings();
    bool ok = false;
    const int configured = settings.value(QStringLiteral("tabs/maxLabelChars"), defaultMaxTabLabelChars)
        .toInt(&ok);
    return ok && configured >= minTabLabelChars && configured <= maxTabLabelChars
        ? configured : defaultMaxTabLabelChars;
}

constexpr const char *backendAuto = "auto";
constexpr const char *backendKonsole = "konsole";
constexpr const char *backendQTerm = "qtermwidget";

QString readBackendPreference() {
    const QString configured = guiSettings().value(QStringLiteral("terminal/backend"),
        QString::fromLatin1(backendAuto)).toString();
    return configured == QString::fromLatin1(backendKonsole)
        || configured == QString::fromLatin1(backendQTerm)
        ? configured : QString::fromLatin1(backendAuto);
}

// Load only the two known application-owned, separately installable modules.
// This keeps Qt6 GUI runnable with either backend installed (no hard link).
class BackendRegistry final {
public:
    BackendRegistry() : BackendRegistry(nullptr) {}
    explicit BackendRegistry(Gpt2CodexTerminalBackend *provider) {
        if (provider) { providers_.insert(provider->id(), provider); return; }
        load(QString::fromLatin1(backendKonsole));
        load(QString::fromLatin1(backendQTerm));
    }
    // Also permits exercising presentation lifecycle with an in-memory backend.
    explicit BackendRegistry(Gpt2CodexTerminalBackend &provider) : BackendRegistry(&provider) {}

    bool available(const QString &id) const {
        auto *provider = providers_.value(id, nullptr);
        return provider && provider->usable();
    }
    QString resolve(const QString &preference) const {
        if (preference == QString::fromLatin1(backendAuto)) {
            if (available(QString::fromLatin1(backendKonsole))) { return QString::fromLatin1(backendKonsole); }
            if (available(QString::fromLatin1(backendQTerm))) { return QString::fromLatin1(backendQTerm); }
            return {};
        }
        return available(preference) ? preference : QString();
    }
    QWidget *create(const QString &preference, QWidget *parent, const QString &program,
                    const QStringList &args, const QString &directory,
                    Gpt2CodexTerminalBackend *&chosen) const {
        chosen = nullptr;
        QStringList candidates;
        if (preference == QString::fromLatin1(backendAuto)) {
            candidates << QString::fromLatin1(backendKonsole) << QString::fromLatin1(backendQTerm);
        } else {
            candidates << preference;
        }
        for (const QString &id : candidates) {
            auto *factory = providers_.value(id, nullptr);
            if (!factory || !factory->usable()) { continue; }
            if (QWidget *view = factory->create(parent, program, args, directory)) {
                chosen = factory;
                return view;
            }
            // Auto falls back only after a genuinely failed Konsole view creation.
        }
        return nullptr;
    }

private:
    void load(const QString &id) {
        const QString filename = QStringLiteral("libgpt2codex-backend-%1.so").arg(id);
        const QStringList directories{
            QCoreApplication::applicationDirPath() + QStringLiteral("/gpt2codex-backends"),
            QString::fromUtf8(G2C_INSTALLED_BACKEND_DIR)
        };
        for (const QString &directory : directories) {
            const QFileInfo file(QDir(directory).filePath(filename));
            if (!file.isFile() || !file.isReadable()) { continue; }
            auto loader = std::make_unique<QPluginLoader>(file.absoluteFilePath());
            loader->setLoadHints(QLibrary::PreventUnloadHint);
            QObject *instance = loader->instance();
            auto *backend = instance ? qobject_cast<Gpt2CodexTerminalBackend *>(instance) : nullptr;
            if (backend && backend->id() == id) {
                providers_.insert(id, backend);
                loaders_.push_back(std::move(loader));
                return;
            }
        }
    }
    QHash<QString, Gpt2CodexTerminalBackend *> providers_;
    std::vector<std::unique_ptr<QPluginLoader>> loaders_;
};

// Strict machine-readable diagnostics only. Never place arbitrary app-server
// data, transcript text or exception payload into a UI widget.
bool validCodexDiagnostics(const QByteArray &raw) {
    if (raw.isEmpty() || raw.size() > 4096) { return false; }
    const QString value = QString::fromUtf8(raw);
    if (value.contains(QChar::ReplacementCharacter)) { return false; }
    const QStringList lines = value.trimmed().split(QLatin1Char('\n'));
    if (lines.size() != 4) { return false; }
    static const QStringList patterns{
        QStringLiteral(R"(^P0J=OK project_id=[a-z0-9_-]{1,64} protocol=stdio metadata=readonly state_db_only=1$)"),
        QStringLiteral(R"(^P0J_SESSIONS=count:[0-9]{1,3} active:[0-9]{1,3} idle:[0-9]{1,3} not_loaded:[0-9]{1,3} system_error:[0-9]{1,3} more:[01]$)"),
        QStringLiteral(R"(^P0J_EVENTS=thread:[0-9]{1,10} turn:[0-9]{1,10} item:[0-9]{1,10} other:[0-9]{1,10} scope=diagnostic_connection_only$)"),
        QStringLiteral(R"(^P0J_TUI=separate_process live_attachment=not_verified control=disabled$)")
    };
    for (int i = 0; i < patterns.size(); ++i) {
        if (!QRegularExpression(patterns.at(i)).match(lines.at(i)).hasMatch()) {
            return false;
        }
    }
    return true;
}

class CodexDiagnosticsPage final : public QWidget {
public:
    explicit CodexDiagnosticsPage(const Options &options, QWidget *parent = nullptr)
        : QWidget(parent), options_(options) {
        auto *layout = new QVBoxLayout(this);
        auto *heading = new QLabel(QStringLiteral("Codex app-server: pouze diagnostika"), this);
        QFont font = heading->font();
        font.setBold(true);
        heading->setFont(font);
        layout->addWidget(heading);
        auto *warning = new QLabel(QStringLiteral(
            "Kontrola spouští oddělené krátkodobé read-only spojení. "
            "Údaje o uložených konverzacích nejsou živým stavem Codexu běžícího v GNU screen. "
            "Události se počítají jen z tohoto diagnostického spojení. Žádné úkoly se neodesílají."), this);
        warning->setWordWrap(true);
        layout->addWidget(warning);
        auto *line = new QHBoxLayout();
        projects_ = new QComboBox(this);
        projects_->setMinimumWidth(180);
        line->addWidget(projects_, 1);
        auto *reload = new QPushButton(QStringLiteral("Načíst projekty"), this);
        line->addWidget(reload);
        check_ = new QPushButton(QStringLiteral("Ověřit app-server"), this);
        check_->setEnabled(false);
        line->addWidget(check_);
        layout->addLayout(line);
        output_ = new QPlainTextEdit(this);
        output_->setReadOnly(true);
        output_->setMaximumBlockCount(30);
        output_->setPlainText(QStringLiteral("Načítám registrované projekty…"));
        layout->addWidget(output_, 1);
        connect(reload, &QPushButton::clicked, this, [this] { loadProjects(); });
        connect(check_, &QPushButton::clicked, this, [this] { runProbe(); });
        QTimer::singleShot(0, this, [this] { loadProjects(); });
    }

    ~CodexDiagnosticsPage() override {
        for (const auto &process : {projectsProcess_, probeProcess_}) {
            if (process && process->state() != QProcess::NotRunning) {
                process->kill();
                process->waitForFinished(200);
            }
        }
    }

private:
    QStringList configArgs() const {
        if (options_.config.isEmpty()) { return {}; }
        return QStringList{QStringLiteral("--config"), options_.config};
    }
    void loadProjects() {
        if (projectsProcess_ || probeProcess_) { return; }
        check_->setEnabled(false);
        const QString previous = projects_->currentData().toString();
        projects_->clear();
        output_->setPlainText(QStringLiteral("Načítám seznam registrovaných projektů…"));
        auto *proc = new QProcess(this);
        projectsProcess_ = proc;
        QStringList args{QStringLiteral("projects"), QStringLiteral("list")};
        args << configArgs();
        proc->setProcessChannelMode(QProcess::SeparateChannels);
        connect(proc, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
                [this, proc, previous](int code, QProcess::ExitStatus status) {
            if (projectsProcess_ != proc) { return; }
            projectsProcess_ = nullptr;
            const QByteArray raw = proc->readAllStandardOutput();
            proc->deleteLater();
            if (code != 0 || status != QProcess::NormalExit || raw.size() > 65536) {
                output_->setPlainText(QStringLiteral("Seznam projektů není dostupný."));
                return;
            }
            static const QRegularExpression idLine(QStringLiteral(R"(^id=([a-z0-9_-]{1,64}) name=.+ root=.+$)"));
            QSet<QString> seen;
            for (const QString &line : QString::fromUtf8(raw).split(QLatin1Char('\n'), Qt::SkipEmptyParts)) {
                const auto match = idLine.match(line);
                if (!match.hasMatch()) { continue; }
                const QString id = match.captured(1);
                if (!seen.contains(id) && seen.size() < 100) {
                    seen.insert(id);
                    projects_->addItem(id, id);
                }
            }
            const int oldIndex = projects_->findData(previous);
            if (oldIndex >= 0) { projects_->setCurrentIndex(oldIndex); }
            check_->setEnabled(projects_->count() > 0);
            output_->setPlainText(projects_->count()
                ? QStringLiteral("Vyber projekt a klikni na Ověřit app-server.")
                : QStringLiteral("Žádný registrovaný projekt není dostupný."));
        });
        connect(proc, &QProcess::errorOccurred, this, [this, proc](QProcess::ProcessError) {
            if (projectsProcess_ != proc) { return; }
            projectsProcess_ = nullptr;
            output_->setPlainText(QStringLiteral("Bridge CLI se nepodařilo spustit."));
            proc->deleteLater();
        });
        proc->start(options_.bridgeCli, args);
        QTimer::singleShot(4000, this, [this, tracked = QPointer<QProcess>(proc)] {
            if (tracked && projectsProcess_ == tracked) { tracked->kill(); }
        });
    }
    void runProbe() {
        if (projectsProcess_ || probeProcess_ || projects_->currentData().toString().isEmpty()) { return; }
        check_->setEnabled(false);
        output_->setPlainText(QStringLiteral("Ověřuji Codex app-server v režimu pouze pro čtení…"));
        auto *proc = new QProcess(this);
        probeProcess_ = proc;
        QStringList args{QStringLiteral("codex"), QStringLiteral("diagnostics"),
                         QStringLiteral("--id"), projects_->currentData().toString()};
        args << configArgs();
        proc->setProcessChannelMode(QProcess::SeparateChannels);
        connect(proc, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
                [this, proc](int code, QProcess::ExitStatus status) {
            if (probeProcess_ != proc) { return; }
            probeProcess_ = nullptr;
            check_->setEnabled(projects_->count() > 0);
            const QByteArray raw = proc->readAllStandardOutput();
            proc->deleteLater();
            if (code == 0 && status == QProcess::NormalExit && validCodexDiagnostics(raw)) {
                output_->setPlainText(QString::fromUtf8(raw));
            } else {
                // Never print stderr, raw RPC errors, previews or arbitrary server data.
                output_->setPlainText(QStringLiteral("Diagnostika selhala nebo vrátila neznámý formát. "
                    "Zkontroluj samostatně příkaz gpt2codex codex diagnostics --id PROJECT_ID."));
            }
        });
        connect(proc, &QProcess::errorOccurred, this, [this, proc](QProcess::ProcessError) {
            if (probeProcess_ != proc) { return; }
            probeProcess_ = nullptr;
            check_->setEnabled(projects_->count() > 0);
            output_->setPlainText(QStringLiteral("Nelze spustit lokální Codex diagnostiku."));
            proc->deleteLater();
        });
        proc->start(options_.bridgeCli, args);
        QTimer::singleShot(55000, this, [this, tracked = QPointer<QProcess>(proc)] {
            if (tracked && probeProcess_ == tracked) { tracked->kill(); }
        });
    }

    Options options_;
    QComboBox *projects_ = nullptr;
    QPushButton *check_ = nullptr;
    QPlainTextEdit *output_ = nullptr;
    QPointer<QProcess> projectsProcess_;
    QPointer<QProcess> probeProcess_;
};

#include "project_multiplexer_settings.h"

class SettingsPage final : public QWidget {
public:
    SettingsPage(const Options &options, const BackendRegistry &backends, const QString &currentBackend,
                 int currentLimit, QWidget *parent = nullptr)
        : QWidget(parent), appliedBackend_(currentBackend), appliedLimit_(currentLimit) {
        auto *outer = new QVBoxLayout(this);
        auto *heading = new QLabel(QStringLiteral("Nastavení gpt2codex"), this);
        QFont headingFont = heading->font();
        headingFont.setPointSize(headingFont.pointSize() + 3);
        headingFont.setBold(true);
        heading->setFont(headingFont);
        outer->addWidget(heading);

        auto *content = new QHBoxLayout();
        auto *categories = new QListWidget(this);
        categories->addItems(QStringList{QStringLiteral("Terminál"), QStringLiteral("Záložky"), QStringLiteral("Codex API")});
        categories->setFixedWidth(165);
        auto *pages = new QStackedWidget(this);

        auto *terminalPage = new QWidget(pages);
        auto *terminalLayout = new QVBoxLayout(terminalPage);
        auto *terminalForm = new QFormLayout();
        backendBox_ = new QComboBox(terminalPage);
        const bool konsole = backends.available(QString::fromLatin1(backendKonsole));
        const bool qterm = backends.available(QString::fromLatin1(backendQTerm));
        backendBox_->addItem(QStringLiteral("Automaticky (preferovat Konsole KPart)"), QString::fromLatin1(backendAuto));
        backendBox_->addItem(konsole ? QStringLiteral("Konsole KPart")
            : QStringLiteral("Konsole KPart (nedostupné)"), QString::fromLatin1(backendKonsole));
        backendBox_->addItem(qterm ? QStringLiteral("QTermWidget")
            : QStringLiteral("QTermWidget (nedostupné)"), QString::fromLatin1(backendQTerm));
        backendBox_->setCurrentIndex(qMax(0, backendBox_->findData(currentBackend)));
        terminalForm->addRow(QStringLiteral("Terminálový backend:"), backendBox_);
        terminalLayout->addLayout(terminalForm);
        auto *terminalHint = new QLabel(QStringLiteral("Konsole: %1. QTermWidget: %2. Přepnutí po Aplikovat odpojí pouze terminálová okna a znovu ověří relace.")
            .arg(konsole ? QStringLiteral("dostupná") : QStringLiteral("nedostupná"),
                 qterm ? QStringLiteral("dostupný") : QStringLiteral("nedostupný")), terminalPage);
        terminalHint->setWordWrap(true);
        terminalLayout->addWidget(terminalHint);
        terminalLayout->addWidget(new ProjectMultiplexerSettings(options, terminalPage));
        terminalLayout->addStretch();
        pages->addWidget(terminalPage);

        auto *tabsPage = new QWidget(pages);
        auto *tabsLayout = new QVBoxLayout(tabsPage);
        auto *tabsForm = new QFormLayout();
        lengthBox_ = new QSpinBox(tabsPage);
        lengthBox_->setRange(minTabLabelChars, maxTabLabelChars);
        lengthBox_->setValue(currentLimit);
        lengthBox_->setSuffix(QStringLiteral(" znaků"));
        tabsForm->addRow(QStringLiteral("Maximální délka názvu:"), lengthBox_);
        tabsLayout->addLayout(tabsForm);
        auto *tabsHint = new QLabel(QStringLiteral("Celý název úkolu zůstane v tooltipu. Záložka Queue Worker je připnutá."), tabsPage);
        tabsHint->setWordWrap(true);
        tabsLayout->addWidget(tabsHint);
        tabsLayout->addStretch();
        pages->addWidget(tabsPage);
        pages->addWidget(new CodexDiagnosticsPage(options, pages));
        content->addWidget(categories);
        content->addWidget(pages, 1);
        outer->addLayout(content, 1);
        connect(categories, &QListWidget::currentRowChanged, pages, &QStackedWidget::setCurrentIndex);
        categories->setCurrentRow(0);

        auto *bottom = new QHBoxLayout();
        note_ = new QLabel(QStringLiteral("Změny se uloží až po potvrzení."), this);
        bottom->addWidget(note_, 1);
        apply_ = new QPushButton(QStringLiteral("Aplikovat"), this);
        apply_->setEnabled(false);
        bottom->addWidget(apply_);
        outer->addLayout(bottom);
        connect(backendBox_, qOverload<int>(&QComboBox::currentIndexChanged), this, [this] { updateDirty(); });
        connect(lengthBox_, qOverload<int>(&QSpinBox::valueChanged), this, [this] { updateDirty(); });
        connect(apply_, &QPushButton::clicked, this, [this] {
            if (applyCallback_ && applyCallback_(selectedBackend(), lengthBox_->value())) {
                appliedBackend_ = selectedBackend();
                appliedLimit_ = lengthBox_->value();
                note_->setText(QStringLiteral("Nastavení uloženo a aplikováno."));
                updateDirty();
            }
        });
    }
    void setApplyCallback(std::function<bool(const QString &, int)> callback) {
        applyCallback_ = std::move(callback);
    }
private:
    QString selectedBackend() const { return backendBox_->currentData().toString(); }
    void updateDirty() {
        const bool changed = selectedBackend() != appliedBackend_ || lengthBox_->value() != appliedLimit_;
        apply_->setEnabled(changed);
        if (changed) { note_->setText(QStringLiteral("Neuložené změny")); }
    }
    QComboBox *backendBox_ = nullptr;
    QSpinBox *lengthBox_ = nullptr;
    QPushButton *apply_ = nullptr;
    QLabel *note_ = nullptr;
    QString appliedBackend_;
    int appliedLimit_ = defaultMaxTabLabelChars;
    std::function<bool(const QString &, int)> applyCallback_;
};

struct Session {
    QString id;
    QString screenName;
    QString state;
    QString sessionId;
    QString task;
    QString multiplexer = QStringLiteral("screen");
    QString generation = {};
    QString programState = QStringLiteral("unknown");
    QString exitCode = QStringLiteral("-");
    QString exitSignal = QStringLiteral("-");
    QString exitedAt = QStringLiteral("-");
    QString windowNumber = QStringLiteral("-");
    QString windowTitle = QStringLiteral("-");
    QString key() const { return multiplexer + QLatin1Char(':') + id; }
    bool running() const { return state == QStringLiteral("attached") || state == QStringLiteral("detached"); }
    bool hasCodexView() const {
        if (!running()) { return false; }
        if (multiplexer == QStringLiteral("tmux")) {
            return programState == QStringLiteral("running") || programState == QStringLiteral("exited");
        }
        return programState == QStringLiteral("running")
            && windowNumber != QStringLiteral("-") && windowTitle == screenName;
    }
};

// A freshly verified screen list must refer to the same recorded process,
// not merely to the same project slot or a reused screen name. The Rust
// bridge still makes the final procfs ownership/identity check on attach.
bool sameLiveSessionIdentity(const Session &previous, const Session &current) {
    return previous.running() && current.running()
        && previous.id == current.id
        && previous.multiplexer == current.multiplexer
        && previous.generation == current.generation
        && previous.screenName == current.screenName
        && previous.sessionId == current.sessionId
        && previous.windowNumber == current.windowNumber
        && previous.windowTitle == current.windowTitle;
}

g2cattention::Identity attentionIdentity(const Session &session) {
    return {session.id, session.multiplexer, session.sessionId, session.generation};
}

QStringList listArguments(const Options &options) {
    QStringList args{QStringLiteral("session"), QStringLiteral("list"), QStringLiteral("--program-status")};
    if (!options.config.isEmpty()) {
        args << QStringLiteral("--config") << options.config;
    }
    return args;
}

QStringList attachArguments(const Options &options, const Session &session) {
    QStringList args{QStringLiteral("session"), QStringLiteral("attach"),
        QStringLiteral("--multiplexer"), session.multiplexer,
        QStringLiteral("--expect-session-id"), session.sessionId};
    if (!session.generation.isEmpty()) {
        args << QStringLiteral("--expect-process-start-ticks") << session.generation;
    }
    if (session.id == QStringLiteral("bridge")) {
        args << QStringLiteral("--bridge");
    } else {
        args << QStringLiteral("--id") << session.id;
    }
    if (session.multiplexer == QStringLiteral("screen") && session.windowNumber != QStringLiteral("-")) {
        args << QStringLiteral("--window") << session.windowNumber;
    }
    if (!options.config.isEmpty()) {
        args << QStringLiteral("--config") << options.config;
    }
    return args;
}

// Deliberately fail closed: unknown/duplicate/malformed identity is never attached.
bool parseSessions(const QByteArray &bytes, QList<Session> &sessions, QString &error) {
    if (bytes.size() > 64 * 1024 || bytes.isEmpty()) {
        error = QStringLiteral("Neplatná délka výstupu session list");
        return false;
    }
    static const QRegularExpression lineExpression(
        QStringLiteral(R"(^(?:multiplexer=(screen|tmux) generation=([0-9]{1,20}|-) )?project_id=([a-z0-9_-]{1,64}) screen_name=(g2c-[a-z0-9_-]{1,68}) state=(attached|detached|interrupted|unconfirmed|unknown|not_registered) session_id=(\S{1,128}) (?:program=(unknown|running|anchor|exited) exit_code=(-|[0-9]{1,3}) exit_signal=(-|[A-Z0-9]{1,16}) exited_at=(-|[0-9]{1,20})(?: window=([0-9]{1,2}|-) window_title=([A-Za-z0-9_-]{1,80}|-))? )?task=(.{1,192})$)"));
    static const QRegularExpression idExpression(QStringLiteral(R"(^[1-9][0-9]{0,9}\.g2c-[a-z0-9_-]{1,64}$)"));
    static const QRegularExpression tmuxIdExpression(QStringLiteral(R"(^[1-9][0-9]{0,9}\.\$[0-9]{1,20}\.[1-9][0-9]{0,19}$)"));
    const QString text = QString::fromUtf8(bytes);
    if (text.contains(QChar::ReplacementCharacter)) {
        error = QStringLiteral("Neplatné UTF-8 v odpovědi bridge");
        return false;
    }
    QSet<QString> ids;
    const QStringList lines = text.split(QLatin1Char('\n'), Qt::SkipEmptyParts);
    if (lines.size() > 200) {
        error = QStringLiteral("Příliš mnoho záznamů session list");
        return false;
    }
    for (const QString &line : lines) {
        const QRegularExpressionMatch match = lineExpression.match(line);
        if (!match.hasMatch()) {
            error = QStringLiteral("Neznámý formát session list; nepřipojuji relace");
            return false;
        }
        Session item{match.captured(3), match.captured(4), match.captured(5), match.captured(6), match.captured(13),
            match.captured(1).isEmpty() ? QStringLiteral("screen") : match.captured(1), match.captured(2)};
        if (!match.captured(7).isEmpty()) {
            item.programState = match.captured(7);
            item.exitCode = match.captured(8);
            item.exitSignal = match.captured(9);
            item.exitedAt = match.captured(10);
            item.windowNumber = match.captured(11).isEmpty() ? QStringLiteral("-") : match.captured(11);
            item.windowTitle = match.captured(12).isEmpty() ? QStringLiteral("-") : match.captured(12);
            const bool tmuxRunning = item.programState == QStringLiteral("running")
                && item.multiplexer == QStringLiteral("tmux") && item.running()
                && item.windowNumber == QStringLiteral("-") && item.windowTitle == QStringLiteral("-");
            const bool terminalExit = item.programState == QStringLiteral("exited")
                && item.multiplexer == QStringLiteral("tmux") && item.running()
                && item.windowNumber == QStringLiteral("-") && item.windowTitle == QStringLiteral("-");
            const bool screenCodex = item.programState == QStringLiteral("running")
                && item.multiplexer == QStringLiteral("screen") && item.running()
                && item.windowNumber != QStringLiteral("-") && item.windowTitle == item.screenName;
            const bool screenAnchor = item.programState == QStringLiteral("anchor")
                && item.multiplexer == QStringLiteral("screen") && item.running()
                && item.windowNumber != QStringLiteral("-") && item.windowTitle == QStringLiteral("g2c-anchor");
            const bool unknown = item.programState == QStringLiteral("unknown")
                && item.exitCode == QStringLiteral("-") && item.exitSignal == QStringLiteral("-")
                && item.exitedAt == QStringLiteral("-")
                && item.windowNumber == QStringLiteral("-") && item.windowTitle == QStringLiteral("-");
            if ((item.exitCode != QStringLiteral("-") && item.exitCode.toInt() > 255)
                || (item.programState != QStringLiteral("exited") && (item.exitCode != QStringLiteral("-") || item.exitSignal != QStringLiteral("-") || item.exitedAt != QStringLiteral("-")))
                || !(tmuxRunning || terminalExit || screenCodex || screenAnchor || unknown)) {
                error = QStringLiteral("Nekonzistentní stav programu nebo screen okna");
                return false;
            }
        }
        if (item.screenName != QStringLiteral("g2c-") + item.id || ids.contains(item.key())) {
            error = QStringLiteral("Nejednoznačná identita relace");
            return false;
        }
        for (const QChar ch : item.task) {
            if (ch.isNull() || ch.isLowSurrogate() || ch.isHighSurrogate() || (ch.isSpace() && ch != QLatin1Char(' ')) || ch.category() == QChar::Other_Control) {
                error = QStringLiteral("Nedovolené znaky v názvu úkolu");
                return false;
            }
        }
        const bool validId = item.multiplexer == QStringLiteral("tmux")
            ? tmuxIdExpression.match(item.sessionId).hasMatch()
            : idExpression.match(item.sessionId).hasMatch() && item.sessionId.endsWith(QStringLiteral(".") + item.screenName);
        if (item.running() && (!validId || (!match.captured(1).isEmpty() && item.generation == QStringLiteral("-")))) {
            error = QStringLiteral("Neplatné ID nebo generace běžící relace");
            return false;
        }
        ids.insert(item.key());
        sessions.append(item);
    }
    if (!ids.contains(QStringLiteral("screen:bridge")) && !ids.contains(QStringLiteral("tmux:bridge"))) {
        error = QStringLiteral("V odpovědi bridge chybí rezervovaná relace Bridge");
        return false;
    }
    return true;
}

QString displayName(const Session &session, int maxLength = defaultMaxTabLabelChars) {
    const QString suffix = QStringLiteral(" [%1]").arg(session.multiplexer);
    if (session.id == QStringLiteral("bridge")) { return QStringLiteral("Bridge") + suffix; }
    const QString task = session.task == QStringLiteral("-") ? QStringLiteral("Codex") : session.task;
    const QString full = session.id + QStringLiteral(" | ") + task;
    const int room = qBound(minTabLabelChars, maxLength, maxTabLabelChars) - suffix.size();
    return (full.size() > room ? full.left(room - 1) + QStringLiteral("…") : full) + suffix;
}

[[maybe_unused]] QString startupBridgeKey(const QList<Session> &sessions) {
    for (const QString &mux : {QStringLiteral("screen"), QStringLiteral("tmux")}) {
        for (const Session &session : sessions) {
            if (session.id == QStringLiteral("bridge") && session.multiplexer == mux && session.running()) {
                return session.key();
            }
        }
    }
    return QStringLiteral("screen:bridge");
}

class SessionPage final : public QWidget {
    Q_OBJECT
public:
    SessionPage(const Options &options, BackendRegistry &backends,
                const QString &preference, const Session &session, QWidget *parent = nullptr)
        : QWidget(parent), options_(options), backends_(backends),
          preference_(preference), session_(session) {
        auto *layout = new QVBoxLayout(this);
        layout->setContentsMargins(4, 4, 4, 4);
        status_ = new QLabel(this);
        status_->setObjectName(QStringLiteral("sessionStatus"));
        status_->setTextFormat(Qt::PlainText);
        layout->addWidget(status_);
        hint_ = new QLabel(this);
        hint_->setTextFormat(Qt::PlainText);
        hint_->setWordWrap(true);
        layout->addWidget(hint_);
        reconnect_ = new QPushButton(QStringLiteral("Znovu připojit terminál"), this);
        reconnect_->setVisible(false);
        layout->addWidget(reconnect_);
        connect(reconnect_, &QPushButton::clicked, this, [this] {
            // A user-approved reconnect is still conditional on the next
            // listing reporting exactly the identity shown at click time.
            if (!session_.hasCodexView()) { return; }
            expectedReattachSessionId_ = session_.sessionId;
            intentionallyDetached_ = false;
            wantAttach_ = true;
            if (refreshRequested_) { refreshRequested_(); }
        });
        if (session.hasCodexView()) { attach(); }
        update(session);
        // The pinned Bridge is created before the first asynchronous listing.
        initialListing_ = session.id == QStringLiteral("bridge")
            && (session.state == QStringLiteral("not_registered") || session.state == QStringLiteral("unknown"));
        qApp->installEventFilter(this);
    }

    void setRefreshRequested(std::function<void()> callback) { refreshRequested_ = std::move(callback); }
    QString tabLabel(int limit) const { return displayName(session_, limit); }
    const Session &session() const { return session_; }
    void setPresentationChanged(std::function<void()> callback) { presentationChanged_ = std::move(callback); }
    bool attentionEnabled() const {
        return terminal_ && session_.hasCodexView() && session_.programState != QStringLiteral("exited")
            && !identityChanged_ && !intentionallyDetached_;
    }

    bool containsTerminal(QWidget *widget) const {
        return terminal_ && widget && (widget == terminal_ || terminal_->isAncestorOf(widget));
    }

    void activateTerminal() {
        if (!terminal_ && session_.hasCodexView() && !intentionallyDetached_ && !identityChanged_ && !wantAttach_) {
            expectedReattachSessionId_ = session_.sessionId;
            wantAttach_ = true;
            if (refreshRequested_) { refreshRequested_(); }
        }
        focusTerminal();
    }

    void disconnectTerminal() {
        intentionallyDetached_ = true;
        initialListing_ = false;
        wantAttach_ = false;
        expectedReattachSessionId_.clear();
        detachTerminal();
        update(session_);
    }

    // QTabWidget gives keyboard focus to the tab bar when switching tabs.
    // A Konsole KPart also wraps its real input surface in intermediate
    // widgets. Focus the painted terminal viewport explicitly, but only
    // when this session page is the visible selected tab. Never synthesize
    // keystrokes or write to the attached PTY.
    void focusTerminal() {
        if (!isVisible() || !terminal_ || !terminal_->isVisible()) { return; }
        QWidget *focusTarget = terminal_;
        for (QWidget *child : terminal_->findChildren<QWidget *>()) {
            if (QString::fromLatin1(child->metaObject()->className())
                    .endsWith(QStringLiteral("TerminalDisplay"))) {
                focusTarget = child;
                break;
            }
        }
        focusTarget->setFocus(Qt::OtherFocusReason);
    }

    // Clipboard paste is an explicit user action against the current tab.
    // Delegate it to the active terminal backend; never inject text through
    // `screen -X stuff`, shell commands or TerminalInterface::sendInput().
    bool pasteClipboardText() {
        if (!session_.running() || !terminal_ || !backend_) { return false; }
        bool accepted = false;
        return QMetaObject::invokeMethod(terminal_, "pasteClipboardText",
            Qt::DirectConnection, Q_RETURN_ARG(bool, accepted)) && accepted;
    }

    void prepareBackendChange(const QString &preference) {
        // Destroy only the transient PTY attachment widget, never a screen
        // session. A NEW listing must still identify the same session PID.
        preference_ = preference;
        const bool restore = terminal_ || wantAttach_;
        expectedReattachSessionId_ = restore && session_.hasCodexView() ? session_.sessionId : QString();
        detachTerminal();
        wantAttach_ = restore && session_.hasCodexView() && !intentionallyDetached_ && !identityChanged_;
        hint_->setText(QStringLiteral("Přepínám backend; ověřuji původní identitu relace…"));
        hint_->show();
        reconnect_->hide();
    }
    void update(const Session &session) {
        if (initialListing_) {
            initialListing_ = false;
            wantAttach_ = session.hasCodexView();
        }
        // Do not silently adopt a replacement session that happens to use
        // the same project ID/name. This also applies to an existing view.
        const bool replacedWhileRunning = session_.running() && session.running()
            && !sameLiveSessionIdentity(session_, session);
        const bool changedDuringReconnect = wantAttach_ && session.running()
            && !expectedReattachSessionId_.isEmpty()
            && expectedReattachSessionId_ != session.sessionId;
        if (replacedWhileRunning || changedDuringReconnect) {
            detachTerminal();
            wantAttach_ = false;
            expectedReattachSessionId_.clear();
            identityChanged_ = true;
        } else if (!session.running()) {
            // An unavailable/unconfirmed session is not an authorization to
            // retain a stale attachment or auto-attach to a later replacement.
            detachTerminal();
            if (session_.running()) { identityChanged_ = true; }
            wantAttach_ = false;
            expectedReattachSessionId_.clear();
        }
        session_ = session;
        status_->setText(QStringLiteral("%1    %2: %3    Stav: %4    Terminál: %5")
            .arg(session.id, session.multiplexer, session.screenName, session.state,
                 backend_ ? backend_->id() : QStringLiteral("odpojen"))
            + QStringLiteral("    Proces: ") + (session.programState == QStringLiteral("exited")
                ? QStringLiteral("ukončen (exit %1, signál %2); bez automatického restartu").arg(session.exitCode, session.exitSignal)
                : session.programState == QStringLiteral("running") ? QStringLiteral("běží") : QStringLiteral("nesledován / neověřen")));
        if (!session.hasCodexView()) {
            const QString target = session.id == "bridge" ? QStringLiteral("--bridge") : "--id " + session.id;
            const QString reason = session.programState == "anchor"
                ? QStringLiteral("Projekt obsahuje pouze kotevní okno; Codex v něm neběží. ")
                : session.state == "not_registered"
                    ? QStringLiteral("Projekt je pouze registrován; aktivní Codex relace neexistuje. ")
                    : QStringLiteral("Aktivní Codex okno není ověřeno. ");
            hint_->setText(reason
                + QStringLiteral("GUI nic nespouští ani nepřebírá. Ověření: %1 session status %2 --multiplexer %3 --config %4")
                    .arg(options_.bridgeCli, target, session.multiplexer,
                         options_.config.isEmpty() ? defaultBridgeConfig() : options_.config));
            hint_->show();
            reconnect_->hide();
        } else if (terminal_) {
            hint_->hide();
            reconnect_->hide();
            reconnect_->setEnabled(true);
        } else if (wantAttach_) {
            expectedReattachSessionId_.clear();
            identityChanged_ = false;
            reconnect_->setEnabled(true);
            attach();
        } else {
            hint_->setText(identityChanged_
                ? QStringLiteral("Identita relace se změnila. Automatické připojení bylo zablokováno; ověř její stav a připoj ji ručně.")
                : QStringLiteral("Terminál je odpojen. Multiplexer a Codex mohou dál běžet."));
            hint_->show();
            reconnect_->show();
            reconnect_->setEnabled(true);
        }
        if (presentationChanged_) { presentationChanged_(); }
    }

protected:
    bool eventFilter(QObject *target, QEvent *event) override {
        if (event->type() == QEvent::KeyPress && containsTerminal(qobject_cast<QWidget *>(target))) {
            const auto *key = static_cast<QKeyEvent *>(event);
            // Observe, never consume, the actual multiplexer's default prefix.
            const int prefix = session_.multiplexer == QStringLiteral("tmux") ? Qt::Key_B : Qt::Key_A;
            if (key->key() == prefix && key->modifiers() == Qt::ControlModifier) {
                screenPrefix_ = !screenPrefix_;
            } else if (key->key() != Qt::Key_Control && key->key() != Qt::Key_Shift) {
                if (screenPrefix_ && key->key() == Qt::Key_D
                    && (key->modifiers() == Qt::NoModifier || key->modifiers() == Qt::ControlModifier)) {
                    intentionallyDetached_ = true;
                    if (presentationChanged_) { presentationChanged_(); }
                }
                screenPrefix_ = false;
            }
        }
        return QWidget::eventFilter(target, event);
    }

    bool event(QEvent *event) override {
        if (terminal_ && backend_ && (event->type() == QEvent::ApplicationPaletteChange
                || event->type() == QEvent::ApplicationFontChange)) {
            backend_->refreshAppearance(terminal_);
        }
        return QWidget::event(event);
    }

private Q_SLOTS:
    void onTerminalFinished() {
        if (sender() != terminal_ || !terminal_) { return; }
        auto *old = terminal_;
        terminal_ = nullptr;
        backend_ = nullptr;
        layout()->removeWidget(old);
        old->deleteLater();
        hint_->setText(QStringLiteral("Terminál byl odpojen. Relace %1 může dál běžet.").arg(session_.multiplexer));
        hint_->show();
        reconnect_->show();
        reconnect_->setEnabled(true);
        wantAttach_ = false; // No automatic repeat after unexpected termination.
        if (presentationChanged_) { presentationChanged_(); }
    }

private:
    void detachTerminal() {
        if (!terminal_) { return; }
        auto *old = terminal_;
        terminal_ = nullptr;
        backend_ = nullptr;
        disconnect(old, nullptr, this, nullptr);
        layout()->removeWidget(old);
        delete old; // Kills only the transient screen-attach client PTY.
        if (presentationChanged_) { presentationChanged_(); }
    }

    void attach() {
        if (terminal_ || !session_.hasCodexView()) { return; }
        screenPrefix_ = false;
        wantAttach_ = false;
        Gpt2CodexTerminalBackend *provider = nullptr;
        QWidget *term = backends_.create(preference_, this, options_.bridgeCli,
            attachArguments(options_, session_), QDir::homePath(), provider);
        if (!term || !provider) {
            hint_->setText(QStringLiteral("Terminálový backend není dostupný. Otevři Nastavení."));
            hint_->show();
            reconnect_->show();
            return;
        }
        terminal_ = term;
        backend_ = provider;
        static_cast<QVBoxLayout *>(layout())->addWidget(term, 1);
        hint_->hide();
        reconnect_->hide();
        // Uniform signal contract is provided by each backend module.
        connect(term, SIGNAL(terminalFinished()), this, SLOT(onTerminalFinished()));
        provider->start(term);
        if (presentationChanged_) { presentationChanged_(); }
        // The PTY and focus proxy can become ready after this widget is
        // inserted into its visible tab; avoid focusing hidden tabs.
        QTimer::singleShot(0, this, [this] { focusTerminal(); });
    }

    Options options_;
    BackendRegistry &backends_;
    QString preference_;
    Session session_;
    QLabel *status_ = nullptr;
    QLabel *hint_ = nullptr;
    QPushButton *reconnect_ = nullptr;
    QWidget *terminal_ = nullptr;
    Gpt2CodexTerminalBackend *backend_ = nullptr;
    bool wantAttach_ = false;
    bool initialListing_ = false;
    bool intentionallyDetached_ = false;
    bool screenPrefix_ = false;
    bool identityChanged_ = false;
    QString expectedReattachSessionId_;
    std::function<void()> refreshRequested_;
    std::function<void()> presentationChanged_;
};

struct QueueScreenRows {
    QList<QJsonObject> rows;
    QStringList warnings;
};

QueueScreenRows parseQueueScreenRows(const QJsonArray &values) {
    QueueScreenRows result;
    QHash<QString, int> counts;
    for (const auto &value : values) {
        const auto slot = value.toObject().value("slot").toString();
        if (!slot.isEmpty()) { ++counts[slot]; }
    }
    int index = 0;
    for (const auto &value : values) {
        ++index;
        auto row = value.toObject();
        const auto slot = row.value("slot").toString();
        auto warn = [&](const QString &reason) {
            result.warnings.append(QStringLiteral("Záznam %1 (%2): %3").arg(index)
                .arg(slot.isEmpty() ? QStringLiteral("bez slotu") : slot, reason));
        };
        if (!value.isObject()) { warn(QStringLiteral("není objekt; vynechán")); continue; }
        if (counts.value(slot) > 1) {
            warn(QStringLiteral("duplicitní identita slotu; všechny výskyty vynechány")); continue;
        }
        const auto match = QRegularExpression(QStringLiteral("^q([0-9]+)-[0-9a-f]{12}$")).match(slot);
        const auto number = row.value("number");
        // #0 can be historical metadata. It is never an attach target.
        if (!match.hasMatch() || !number.isDouble() || number.toDouble() < 0
            || number.toDouble() != number.toInt(-1)
            || match.captured(1) != QString::number(number.toInt())) {
            warn(QStringLiteral("neplatný slot/number nebo nesoulad identity; vynechán")); continue;
        }
        if (!QRegularExpression(QStringLiteral("^[a-zA-Z0-9_-]{1,64}$")).match(row.value("project").toString()).hasMatch()) {
            warn(QStringLiteral("chybějící/neplatný project; vynechán")); continue;
        }
        bool invalid = false;
        for (const auto &key : {QStringLiteral("phase"), QStringLiteral("thread_id"), QStringLiteral("attachable"), QStringLiteral("screen_alive")}) {
            const auto field = row.value(key);
            if (field.isUndefined() || (key == "thread_id" && field.isNull())) { continue; }
            if (key == "attachable" || key == "screen_alive" ? !field.isBool() : !field.isString()) { invalid = true; }
        }
        if (invalid) { warn(QStringLiteral("neplatný typ optional metadat; vynechán")); continue; }
        if (row.value("phase").toString().isEmpty()) {
            row.insert("phase", "metadata_incomplete");
            warn(QStringLiteral("chybí phase; pouze zobrazení"));
        }
        if (!row.contains("attachable")) { warn(QStringLiteral("chybí attachable; připojení zakázáno")); }
        row.insert("key", slot);
        result.rows.append(row);
    }
    return result;
}

bool activeQueueRow(const QJsonObject &row) {
    // A surviving legacy screen window is not proof of an active Queue job.
    // The Queue status is authoritative for the Active/History filter.
    const auto status = row.value("queue_status").toString();
    return status == "pending" || status == "approved" || status == "approved-screen"
        || status == "approved-project-screen" || status == "blocked"
        || status == "running" || status == "unknown";
}

// GitHub Queue is an operator inbox. It never creates a Codex terminal,
// changes a screen window or starts a task. An explicit GUI approval is bound
// to the complete freshly verified remote Issue SHA-256 by the local CLI.
class QueuePage final : public QWidget {
    Q_OBJECT
public:
    QueuePage(const Options &options, BackendRegistry &, QWidget *parent = nullptr)
        : QWidget(parent), options_(options),
          python_(QStandardPaths::findExecutable(QStringLiteral("python3"))) {
        setObjectName(QStringLiteral("githubQueuePage"));
        auto *outer = new QVBoxLayout(this);
        auto *split = new QSplitter(Qt::Horizontal, this);
        split->setObjectName(QStringLiteral("queueTaskSplitter"));
        split->setChildrenCollapsible(false);

        auto *left = new QWidget(split);
        auto *leftLayout = new QVBoxLayout(left);
        leftLayout->setContentsMargins(0, 0, 0, 0);
        left->setMinimumWidth(250);
        auto *top = new QHBoxLayout;
        filter_ = new QComboBox(left);
        filter_->setObjectName(QStringLiteral("queueTaskFilter"));
        filter_->addItems({QStringLiteral("Aktivní"), QStringLiteral("Historie")});
        top->addWidget(filter_, 1);
        auto *refreshButton = new QPushButton(QStringLiteral("Obnovit"), left);
        refreshButton->setObjectName(QStringLiteral("queueTaskRefresh"));
        top->addWidget(refreshButton);
        leftLayout->addLayout(top);
        list_ = new QListWidget(left);
        list_->setObjectName(QStringLiteral("queueTaskList"));
        list_->setWordWrap(false);
        list_->setTextElideMode(Qt::ElideRight);
        leftLayout->addWidget(list_, 1);

        auto *right = new QWidget(split);
        right->setMinimumWidth(320);
        auto *rightLayout = new QVBoxLayout(right);
        rightLayout->setContentsMargins(0, 0, 0, 0);
        header_ = new QLabel(QStringLiteral("Vyber úlohu"), right);
        header_->setObjectName(QStringLiteral("queueTaskHeader"));
        header_->setWordWrap(true);
        rightLayout->addWidget(header_);
        metadata_ = new QLabel(right);
        metadata_->setObjectName(QStringLiteral("queueTaskDetail"));
        metadata_->setWordWrap(true);
        metadata_->setTextInteractionFlags(Qt::TextSelectableByMouse);
        rightLayout->addWidget(metadata_);
        task_ = new QPlainTextEdit(right);
        task_->setObjectName(QStringLiteral("queueTaskBody"));
        task_->setReadOnly(true);
        task_->setPlaceholderText(QStringLiteral("Úplné zadání vybrané úlohy se načte z GitHub Queue."));
        rightLayout->addWidget(task_, 1);
        approval_ = new QCheckBox(QStringLiteral("Zkontroloval jsem celé zadání a schvaluji jeho provedení."), right);
        approval_->setObjectName(QStringLiteral("queueTaskApproval"));
        approval_->setEnabled(false);
        rightLayout->addWidget(approval_);
        auto *actions = new QHBoxLayout;
        action_ = new QComboBox(right);
        action_->setObjectName(QStringLiteral("queueTaskAction"));
        action_->addItem(QStringLiteral("Schválit"), QStringLiteral("approve"));
        action_->addItem(QStringLiteral("Zamítnout"), QStringLiteral("reject"));
        action_->addItem(QStringLiteral("Pokračovat"), QStringLiteral("resume"));
        action_->addItem(QStringLiteral("Zahodit"), QStringLiteral("discard"));
        action_->addItem(QStringLiteral("Více informací"), QStringLiteral("info"));
        actions->addWidget(action_, 1);
        confirm_ = new QPushButton(QStringLiteral("OK"), right);
        confirm_->setObjectName(QStringLiteral("queueTaskConfirm"));
        confirm_->setEnabled(false);
        actions->addWidget(confirm_);
        rightLayout->addLayout(actions);
        split->setSizes({410, 690});
        outer->addWidget(split, 1);
        hint_ = new QLabel(this);
        hint_->setObjectName(QStringLiteral("queueTaskHint"));
        hint_->setWordWrap(true);
        outer->addWidget(hint_);

        connect(refreshButton, &QPushButton::clicked, this, &QueuePage::refresh);
        connect(filter_, &QComboBox::currentIndexChanged, this, [this] { applyFilter(); });
        connect(list_, &QListWidget::currentItemChanged, this, [this] {
            loadSelectedDetail();
        });
        connect(approval_, &QCheckBox::toggled, this, [this] { updateAction(); });
        connect(action_, &QComboBox::currentIndexChanged, this, [this] {
            const auto op = action_->currentData().toString();
            approval_->setText(op == QStringLiteral("approve")
                ? QStringLiteral("Zkontroloval jsem celé zadání a schvaluji jeho provedení.")
                : op == QStringLiteral("reject")
                ? QStringLiteral("Potvrzuji zamítnutí vybrané úlohy.")
                : op == QStringLiteral("resume")
                ? QStringLiteral("Potvrzuji opakovaný pokus dosud nepřevzaté úlohy.")
                : op == QStringLiteral("discard")
                ? QStringLiteral("Potvrzuji zahození nepřevzaté úlohy. Pracovní soubory zůstanou.")
                : QStringLiteral("Více informací pouze zobrazí lokální stav."));
            approval_->setChecked(false);
            updateAction();
        });
        connect(confirm_, &QPushButton::clicked, this, &QueuePage::performAction);
        timer_.setInterval(6000);
        connect(&timer_, &QTimer::timeout, this, &QueuePage::refresh);
        timer_.start();
        QTimer::singleShot(0, this, &QueuePage::refresh);
    }
    ~QueuePage() override {
        for (auto *proc : findChildren<QProcess *>(QStringLiteral("queueMetadataQuery"))) {
            disconnect(proc, nullptr, this, nullptr);
            if (proc->state() != QProcess::NotRunning) {
                proc->kill();
                proc->waitForFinished(1000);
            }
        }
    }
    // Compatibility with the main window's terminal-only keyboard/menu paths.
    // Queue itself has no terminal widget or implicit connection side effect.
    bool containsTerminal(QWidget *) const { return false; }
    void focusTerminal() {}
    bool pasteClipboardText() { return false; }
    void disconnectTerminal() {}

    void refresh() {
        if (busy_ || acting_) { return; }
        if (!options_.queueRootError.isEmpty()) {
            hint_->setText(options_.queueRootError);
            return;
        }
        if (options_.queueHelper.isEmpty() || python_.isEmpty()) {
            hint_->setText(QStringLiteral("Queue helper nebo Python 3 nejsou dostupné."));
            return;
        }
        busy_ = true;
        query(options_.queueHelper, {QStringLiteral("list")}, [this](bool ok, const QByteArray &raw) {
            const QJsonDocument doc = QJsonDocument::fromJson(raw);
            if (!ok || !doc.isArray()) {
                busy_ = false;
                hint_->setText(QStringLiteral("Lokální metadata Queue nejsou dostupná."));
                return;
            }
            const auto screens = parseQueueScreenRows(doc.array());
            auto rows = screens.rows;
            screenWarnings_ = screens.warnings;
            const auto cli = queueCli();
            if (!QFileInfo(cli).isFile()) { finishRefresh(rows, false); return; }
            query(cli, {QStringLiteral("gui-list")}, [this, rows](bool jobsOk, const QByteArray &jobsRaw) mutable {
                const auto screenRows = rows;
                const auto doc = QJsonDocument::fromJson(jobsRaw);
                bool parsed = jobsOk && doc.isArray();
                if (parsed) {
                    for (const auto &entry : doc.array()) {
                        if (!entry.isObject()) { parsed = false; break; }
                        const auto job = entry.toObject();
                        const int number = job.value("number").toInt(-1);
                        const QString project = job.value("project").toString();
                        const QString status = job.value("status").toString();
                        const QString title = job.value("title").toString();
                        if (number <= 0 || project.isEmpty() || status.isEmpty() || title.isEmpty()) {
                            parsed = false; break;
                        }
                        bool found = false;
                        for (auto &row : rows) {
                            if (row.value("number").toInt() != number) { continue; }
                            row.insert("title", title);
                            row.insert("queue_status", job.value("display_status").toString(status));
                            row.insert("mode", job.value("mode"));
                            row.insert("block", job.value("block"));
                            found = true;
                        }
                        if (!found) {
                            rows.append(QJsonObject{{"key", QStringLiteral("issue:%1").arg(number)},
                                {"number", number}, {"project", project}, {"title", title},
                                {"mode", job.value("mode")},
                                {"queue_status", job.value("display_status").toString(status)},
                                {"block", job.value("block")}, {"attachable", false}});
                        }
                    }
                }
                finishRefresh(parsed ? rows : screenRows, parsed);
            });
        });
    }
private:
    QString queueCli() const {
        return QFileInfo(options_.queueHelper).dir().filePath(QStringLiteral("gpt2codex-github-queue.py"));
    }
    void query(const QString &script, const QStringList &args,
               std::function<void(bool, const QByteArray &)> callback) {
        auto *proc = new QProcess(this);
        proc->setObjectName(QStringLiteral("queueMetadataQuery"));
        auto complete = std::make_shared<bool>(false);
        auto done = [proc, complete, callback](bool ok) {
            if (*complete) { return; }
            *complete = true;
            const auto raw = proc->readAllStandardOutput();
            callback(ok && raw.size() <= 1024 * 1024, raw);
            proc->deleteLater();
        };
        connect(proc, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
            [done](int code, QProcess::ExitStatus status) {
                done(code == 0 && status == QProcess::NormalExit);
            });
        connect(proc, &QProcess::errorOccurred, this,
            [done](QProcess::ProcessError) { done(false); });
        QTimer::singleShot(script == queueCli() && args.value(0) != QStringLiteral("gui-list") ? 15000 : 5000, proc, [proc] {
            if (proc->state() != QProcess::NotRunning) { proc->kill(); }
        });
        QStringList argv{QStringLiteral("-B"), script};
        if (script == queueCli() && !options_.queueConfig.isEmpty()) {
            argv << QStringLiteral("--config") << options_.queueConfig;
        }
        if (!options_.queueRepoRoot.isEmpty()) {
            argv << QStringLiteral("--repo-root") << options_.queueRepoRoot;
        }
        argv << args;
        proc->start(python_, argv);
    }
    void finishRefresh(const QList<QJsonObject> &rows, bool jobsOk) {
        const QSignalBlocker block(list_);
        QSet<QString> keep;
        for (const auto &row : rows) {
            const auto key = row.value("key").toString();
            keep.insert(key);
            QListWidgetItem *item = nullptr;
            for (int i = 0; i < list_->count(); ++i) {
                if (list_->item(i)->data(Qt::UserRole).toString() == key) {
                    item = list_->item(i); break;
                }
            }
            if (!item) { item = new QListWidgetItem(list_); item->setData(Qt::UserRole, key); }
            const QString project = row.value("project").toString();
            QString title = row.value("title").toString(row.value("slot").toString());
            if (title.startsWith(QStringLiteral("[g2c] "))) { title.remove(0, 6); }
            if (title.startsWith(project + QStringLiteral(": "))) { title.remove(0, project.size() + 2); }
            const QString text = QStringLiteral("#%1    %2    %3\n%4")
                .arg(row.value("number").toInt())
                .arg(project, row.value("queue_status").toString("—"), title);
            item->setToolTip(row.value("title").toString());
            item->setText(text);
            item->setData(Qt::UserRole + 1, row);
        }
        for (int i = list_->count() - 1; i >= 0; --i) {
            if (!keep.contains(list_->item(i)->data(Qt::UserRole).toString())) {
                delete list_->takeItem(i);
            }
        }
        busy_ = false;
        applyFilter();
        hint_->setText(jobsOk ? QStringLiteral("Lokální stav Queue. Detail se ověřuje proti GitHubu při výběru.")
            : QStringLiteral("Lokální Queue list není dostupný; schvalování je vypnuto."));
        if (!screenWarnings_.isEmpty()) {
            hint_->setText(hint_->text() + '\n' + screenWarnings_.join('\n'));
        }
    }
    void applyFilter() {
        for (int i = 0; i < list_->count(); ++i) {
            auto *item = list_->item(i);
            item->setHidden(filter_->currentIndex() == 0
                && !activeQueueRow(item->data(Qt::UserRole + 1).toJsonObject()));
        }
        if (list_->currentItem() && list_->currentItem()->isHidden()) {
            list_->setCurrentItem(nullptr);
        }
        updateAction();
    }
    QJsonObject selected() const {
        if (!list_->currentItem() || list_->currentItem()->isHidden()) { return {}; }
        return list_->currentItem()->data(Qt::UserRole + 1).toJsonObject();
    }
    void loadSelectedDetail() {
        ++selectionEpoch_;
        const auto row = selected();
        verified_ = {};
        approval_->setChecked(false);
        task_->clear();
        header_->setText(row.isEmpty() ? QStringLiteral("Vyber úlohu")
            : QStringLiteral("#%1 · %2").arg(row.value("number").toInt()).arg(row.value("title").toString()));
        QString localStatus = row.isEmpty() ? QString() : QStringLiteral("Projekt: %1 · Stav: %2 · Režim: %3")
            .arg(row.value("project").toString(), row.value("queue_status").toString(), row.value("mode").toString());
        const auto block = row.value("block").toObject();
        if (!block.isEmpty()) {
            localStatus += QStringLiteral("\nDůvod: %1 (%2)")
                .arg(block.value("message").toString(), block.value("code").toString());
        }
        metadata_->setText(localStatus);
        updateAction();
        if (row.isEmpty() || row.value("number").toInt() <= 0 || !QFileInfo(queueCli()).isFile()) { return; }
        const int number = row.value("number").toInt();
        const int epoch = selectionEpoch_;
        task_->setPlainText(QStringLiteral("Načítám úplné zadání…"));
        query(queueCli(), {QStringLiteral("gui-detail"), QString::number(number)},
            [this, number, epoch](bool ok, const QByteArray &bytes) {
                if (epoch != selectionEpoch_ || selected().value("number").toInt() != number) { return; }
                const auto doc = QJsonDocument::fromJson(bytes);
                const auto obj = doc.object();
                if (!ok || !doc.isObject() || obj.value("number").toInt() != number
                    || obj.value("digest").toString().size() != 64) {
                    task_->setPlainText(QStringLiteral("Celé zadání se nepodařilo ověřit proti GitHubu. Schválení není dostupné.\n"
                        "Zkontroluj název [g2c], stav Issue a lokální Queue."));
                    verified_ = {};
                } else {
                    verified_ = obj;
                    header_->setText(QStringLiteral("#%1 · %2").arg(number).arg(obj.value("title").toString()));
                    task_->setPlainText(obj.value("task").toString());
                    QString info = QStringLiteral("Projekt: %1 · Stav: %2 · Režim: %3\nSHA-256: %4")
                        .arg(obj.value("project").toString(), obj.value("status").toString(),
                             obj.value("mode").toString(), obj.value("digest").toString());
                    const auto block = obj.value("block").toObject();
                    if (!block.isEmpty()) {
                        info += QStringLiteral("\nČeká na prostředí: %1 (%2)")
                            .arg(block.value("message").toString(), block.value("code").toString());
                    } else if (obj.value("review_state").toString() == QStringLiteral("claimed_unverified")) {
                        info += QStringLiteral("\nÚloha už byla převzata. Stav vykonávání vyžaduje ruční ověření; automatické Resume není dostupné.");
                    }
                    metadata_->setText(info);
                }
                approval_->setChecked(false);
                updateAction();
            });
    }
    void updateAction() {
        const bool visible = selected().value("number").toInt() > 0 && !acting_;
        const bool same = visible && verified_.value("number").toInt() > 0
            && selected().value("number").toInt() == verified_.value("number").toInt();
        const auto status = verified_.value("status").toString();
        const auto op = action_->currentData().toString();
        const bool open = verified_.value("remote_state").toString() == QStringLiteral("open");
        const bool pending = same && open && status == QStringLiteral("pending");
        const bool unclaimed = same && open && status == QStringLiteral("approved-project-screen");
        const bool allowed = op == QStringLiteral("approve") || op == QStringLiteral("reject")
            ? pending : op == QStringLiteral("resume")
            ? unclaimed && verified_.value("block").isObject()
            : op == QStringLiteral("discard") ? unclaimed
            : op == QStringLiteral("info") ? visible : false;
        action_->setEnabled(visible);
        approval_->setEnabled(allowed && op != QStringLiteral("info"));
        confirm_->setEnabled(allowed && (op == QStringLiteral("info") || approval_->isChecked()));
    }
    void performAction() {
        const auto local = selected();
        const auto job = verified_.isEmpty() ? local : verified_;
        if (acting_ || job.value("number").toInt() <= 0
            || local.value("number").toInt() != job.value("number").toInt()) { return; }
        const int number = job.value("number").toInt();
        const auto action = action_->currentData().toString();
        if (action == QStringLiteral("info")) {
            const auto block = job.value("block").toObject();
            QString detail = QStringLiteral("Stav: %1\nProjekt: %2\nBackend: %3\n")
                .arg(job.value("status").toString(job.value("queue_status").toString()),
                     job.value("project").toString(), job.value("approved_backend").toString("—"));
            if (!block.isEmpty()) {
                detail += QStringLiteral("Důvod: %1\nKód: %2\n")
                    .arg(block.value("message").toString(), block.value("code").toString());
            }
            if (job.value("review_state").toString() == QStringLiteral("claimed_unverified")) {
                detail += QStringLiteral("Úloha mohla již vykonávat změny. Obnova původního Codex turnu není ověřená.\n");
            }
            detail += QStringLiteral("Vytvořený thread: %1\nQueue slot: %2")
                .arg(job.value("thread_id").toString("—"), job.value("screen_slot").toString("—"));
            QMessageBox::information(this, QStringLiteral("Queue úloha #%1").arg(number), detail);
            return;
        }
        if (!approval_->isChecked() || !confirm_->isEnabled()) { return; }
        const auto status = job.value("status").toString();
        if ((action == QStringLiteral("approve") || action == QStringLiteral("reject"))
                && status != QStringLiteral("pending")) { return; }
        if ((action == QStringLiteral("resume") || action == QStringLiteral("discard"))
                && status != QStringLiteral("approved-project-screen")) { return; }
        if ((action == QStringLiteral("reject") || action == QStringLiteral("discard"))
                && QMessageBox::question(this, QStringLiteral("Potvrzení úlohy"),
                    QStringLiteral("Opravdu zahodit nebo zamítnout úlohu #%1? Soubory projektu zůstanou beze změny.").arg(number)) != QMessageBox::Yes) { return; }
        QStringList args;
        if (action == QStringLiteral("approve")) {
            args = {QStringLiteral("gui-approve"), QString::number(number),
                QStringLiteral("--digest"), job.value("digest").toString(),
                QStringLiteral("--confirmation"), QStringLiteral("SCHVALUJI %1").arg(number)};
        } else if (action == QStringLiteral("reject")) {
            args = {QStringLiteral("reject"), QString::number(number)};
        } else if (action == QStringLiteral("resume") || action == QStringLiteral("discard")) {
            args = {QStringLiteral("operator-action"), QString::number(number),
                QStringLiteral("--action"), action,
                QStringLiteral("--digest"), job.value("digest").toString(),
                QStringLiteral("--confirmation"), action.toUpper() + QStringLiteral(" %1").arg(number)};
        } else { return; }
        acting_ = true;
        updateAction();
        query(queueCli(), args, [this](bool ok, const QByteArray &) {
            acting_ = false;
            approval_->setChecked(false);
            verified_ = {};
            hint_->setText(ok ? QStringLiteral("Operace potvrzena lokálním Queue CLI.")
                : QStringLiteral("Operace odmítnuta; zkontroluj stav úlohy, přístup ke GitHubu a Queue log."));
            updateAction();
            refresh();
            loadSelectedDetail();
        });
    }
    Options options_;
    QString python_;
    QStringList screenWarnings_;
    QListWidget *list_ = nullptr;
    QComboBox *filter_ = nullptr, *action_ = nullptr;
    QLabel *header_ = nullptr, *metadata_ = nullptr, *hint_ = nullptr;
    QPlainTextEdit *task_ = nullptr;
    QCheckBox *approval_ = nullptr;
    QPushButton *confirm_ = nullptr;
    QJsonObject verified_;
    QTimer timer_;
    int selectionEpoch_ = 0;
    bool busy_ = false, acting_ = false;
};

// Queue Codex runs via `codex exec --json`, not an interactive TUI.  This
// project tab displays only sanitized event *categories* from the local queue
// metadata API. It never attaches to screen, accesses raw output or starts work.
class QueueActivityPage final : public QWidget {
    Q_OBJECT
public:
    explicit QueueActivityPage(int issue, QWidget *parent = nullptr)
        : QWidget(parent), number_(issue) {
        setObjectName(QStringLiteral("queueActivityPage"));
        auto *layout = new QVBoxLayout(this);
        header_ = new QLabel(this);
        header_->setObjectName(QStringLiteral("queueActivityHeader"));
        header_->setWordWrap(true);
        layout->addWidget(header_);
        state_ = new QLabel(this);
        state_->setObjectName(QStringLiteral("queueActivityState"));
        state_->setWordWrap(true);
        state_->setTextFormat(Qt::PlainText);
        layout->addWidget(state_);
        events_ = new QPlainTextEdit(this);
        events_->setObjectName(QStringLiteral("queueActivityEvents"));
        events_->setReadOnly(true);
        events_->setPlaceholderText(QStringLiteral("Čekám na první události Codexu…"));
        layout->addWidget(events_, 1);
        auto *note = new QLabel(QStringLiteral("Pouze typy událostí. Příkazy, výstupy, prompty a obsah souborů se nezobrazují."), this);
        note->setWordWrap(true);
        layout->addWidget(note);
    }
    int number() const { return number_; }
    QString project() const { return project_; }
    QString title() const { return title_; }
    void update(const QJsonObject &obj) {
        if (obj.value("number").toInt() != number_) { return; }
        project_ = obj.value("project").toString();
        title_ = obj.value("title").toString();
        header_->setText(QStringLiteral("#%1 · %2\nProjekt: %3")
            .arg(number_).arg(title_, project_));
        const QString phase = obj.value("phase").toString("unverified");
        const QHash<QString, QString> phaseNames{
            {QStringLiteral("claim_pending"), QStringLiteral("Příprava spuštění")},
            {QStringLiteral("issue_accepted"), QStringLiteral("Úloha převzata")},
            {QStringLiteral("exec_starting"), QStringLiteral("Spouštění Codexu")},
            {QStringLiteral("turn_running"), QStringLiteral("Codex zpracovává úlohu")},
            {QStringLiteral("turn_finished"), QStringLiteral("Zpracování skončilo")},
            {QStringLiteral("unverified"), QStringLiteral("Stav nelze ověřit")},
        };
        const QString runtime = obj.value("runtime_verified").toBool()
            ? QStringLiteral("Ověřeno okno / screen server; proces Codexu se samostatně neověřuje")
            : QStringLiteral("Runtime neověřen; stav Queue sám o sobě nedokazuje běh Codexu");
        state_->setText(QStringLiteral("Queue: %1 · Backend: %2 · Fáze: %3\n%4 · Turn: %5/%6")
            .arg(obj.value("queue_status").toString(), obj.value("backend").toString(), phaseNames.value(phase, phase), runtime)
            .arg(obj.value("turns_completed").toInt()).arg(obj.value("turns_started").toInt()));
        QStringList entries;
        for (const QJsonValue &value : obj.value("events").toArray()) {
            if (value.isString()) { entries << value.toString(); }
        }
        const QString content = entries.join('\n');
        if (events_->toPlainText() != content) { events_->setPlainText(content); }
    }
private:
    int number_;
    QString project_, title_;
    QLabel *header_ = nullptr, *state_ = nullptr;
    QPlainTextEdit *events_ = nullptr;
};

class MainWindow final : public QMainWindow {
public:
    explicit MainWindow(Options options, Gpt2CodexTerminalBackend *testBackend = nullptr,
                        g2cattention::AttentionSource *source = nullptr)
        : options_(std::move(options)), backends_(testBackend),
          markerAttention_(options_.queueOnly ? QString() : qEnvironmentVariable("XDG_RUNTIME_DIR")),
          attentionSource_(source ? source : &markerAttention_) {
        attentionClock_.start();
        setWindowTitle(QStringLiteral("gpt2codex – správce Codex relací"));
        resize(1100, 680);
        setMinimumSize(700, 420);
        tabs_ = new QTabWidget(this);
        tabs_->setTabsClosable(true);
        tabs_->setMovable(false);
        tabs_->setElideMode(Qt::ElideRight);
        tabs_->tabBar()->setExpanding(false);
        tabs_->tabBar()->setUsesScrollButtons(true);
        setCentralWidget(tabs_);
        const QString stateBase = qEnvironmentVariable("XDG_STATE_HOME", QDir::homePath() + "/.local/state");
        statusBar()->setToolTip(QStringLiteral("CLI: %1\nConfig: %2\nRegistry relací: %3/gpt2codex/screen-sessions\nGUI čte jen registrované identity; seznam fyzických socketů není seznam projektů.")
            .arg(options_.bridgeCli, options_.config.isEmpty() ? defaultBridgeConfig() : options_.config, stateBase));
        queuePage_ = new QueuePage(options_, backends_, tabs_);
        if (options_.queueOnly) {
            tabs_->addTab(queuePage_, QStringLiteral("GitHub Queue"));
            tabs_->setTabsClosable(false);
            auto *action = menuBar()->addAction(QStringLiteral("GitHub Queue úlohy…"));
            connect(action, &QAction::triggered, this, [this] { showQueueTasks(); });
            // Diagnostic mode never discovers or attaches project sessions.
            return; // No Bridge discovery, polling, attention binding or automatic attach.
        }
        allowlistProblem_ = new QLabel(this);
        allowlistProblem_->setObjectName(QStringLiteral("queueAllowlistProblem"));
        allowlistProblem_->setTextFormat(Qt::PlainText);
        allowlistProblem_->setWordWrap(true);
        statusBar()->addPermanentWidget(allowlistProblem_, 1);
        allowlistProblem_->hide();
        attentionSource_->setBindingsChanged([window = QPointer<MainWindow>(this)] {
            if (window) { window->updateTabPresentations(); }
        });
        connect(tabs_, &QTabWidget::currentChanged, this, [this](int index) {
            if (pasteAction_) {
                pasteAction_->setEnabled(qobject_cast<SessionPage *>(tabs_->widget(index)) != nullptr);
            }
            auto *page = qobject_cast<SessionPage *>(tabs_->widget(index));
            if (page) {
                QTimer::singleShot(0, page, [this, page] {
                    if (tabs_->currentWidget() == page) { page->activateTerminal(); }
                });
            }
        });
        connect(tabs_, &QTabWidget::tabCloseRequested, this, [this](int index) {
            if (index <= 0) { return; } // The Queue Worker tab is pinned.
            auto *page = tabs_->widget(index);
            if (page == queuePage_) { return; } // The operator inbox is pinned.
            if (page == settingsPage_) {
                settingsPage_ = nullptr;
                tabs_->removeTab(index);
                page->deleteLater();
                return;
            }
            if (auto *activity = qobject_cast<QueueActivityPage *>(page)) {
                hiddenQueueActivity_.insert(activity->number());
                removeQueueActivityPage(activity->number());
                statusBar()->showMessage(QStringLiteral("Pohled skryt; Queue Codex pokračuje."), 5000);
                return;
            }
            const QString id = pages_.key(page);
            if (id.isEmpty()) { return; }
            hidden_.insert(id);
            attentionSource_->unwatch(attention_.ticket(id));
            attention_.remove(id);
            pages_.remove(id);
            tabs_->removeTab(index);
            page->deleteLater(); // Only disconnects the view; never kills a screen session.
            statusBar()->showMessage(QStringLiteral("Záložka odpojena; persistentní relace pokračuje."), 5000);
        });
        auto *tools = menuBar()->addMenu(QStringLiteral("Relace"));
        if (!options_.queueHelper.isEmpty()) {
            auto *queueAction = tools->addAction(QStringLiteral("GitHub Queue úlohy…"));
            connect(queueAction, &QAction::triggered, this, [this] { showQueueTasks(); });
        }
        auto *refreshAction = tools->addAction(QStringLiteral("Obnovit seznam"));
        connect(refreshAction, &QAction::triggered, this, [this] { refreshSessions(); });
        auto *restoreAction = tools->addAction(QStringLiteral("Zobrazit skryté záložky"));
        connect(restoreAction, &QAction::triggered, this, [this] {
            hidden_.clear();
            hiddenQueueActivity_.clear();
            refreshSessions();
            refreshQueueActivity();
        });
        auto *quitAction = tools->addAction(QStringLiteral("Ukončit pouze GUI"));
        connect(quitAction, &QAction::triggered, this, [this] { quitGui(); });
        auto *detachAction = tools->addAction(QStringLiteral("Odpojit aktuální terminál"));
        connect(detachAction, &QAction::triggered, this, [this] {
            if (auto *page = qobject_cast<SessionPage *>(tabs_->currentWidget())) {
                page->disconnectTerminal();
            }
        });

        // Embedded KParts do not inherit a standalone Konsole main window's
        // Edit menu shortcuts. Route terminal paste through the backend's
        // native clipboard implementation, not as raw terminal key events.
        auto *editMenu = menuBar()->addMenu(QStringLiteral("Upravit"));
        pasteAction_ = editMenu->addAction(QStringLiteral("Vložit text ze schránky"));
        pasteAction_->setShortcuts({QKeySequence(Qt::CTRL | Qt::SHIFT | Qt::Key_V),
                                    QKeySequence(Qt::SHIFT | Qt::Key_Insert)});
        pasteAction_->setShortcutContext(Qt::WindowShortcut);
        pasteAction_->setEnabled(false);
        new PasteShortcutFilter(this, [this](QWidget *target) {
            auto *page = qobject_cast<SessionPage *>(tabs_->currentWidget());
            return page && page->containsTerminal(target);
        }, [this] { pasteAction_->trigger(); });
        connect(pasteAction_, &QAction::triggered, this, [this] {
            const QMimeData *mime = QGuiApplication::clipboard()->mimeData(QClipboard::Clipboard);
            if (!mime || !mime->hasText()) {
                statusBar()->showMessage(QStringLiteral("Ve schránce není text."), 5000);
                return;
            }
            auto *page = qobject_cast<SessionPage *>(tabs_->currentWidget());
            const bool pasted = page && page->pasteClipboardText();
            if (!pasted) {
                statusBar()->showMessage(QStringLiteral("Terminál neumožnil vložení. Ověř backend a připojení relace."), 7000);
            }
        });

        auto *settingsMenu = menuBar()->addMenu(QStringLiteral("Nastavení"));
        auto *openSettingsAction = settingsMenu->addAction(QStringLiteral("Otevřít nastavení"));
        connect(openSettingsAction, &QAction::triggered, this, [this] { showSettingsTab(); });

        trayAvailable_ = QSystemTrayIcon::isSystemTrayAvailable();
        if (trayAvailable_) {
            const QIcon icon = QIcon::fromTheme(QStringLiteral("utilities-terminal"),
                style()->standardIcon(QStyle::SP_ComputerIcon));
            setWindowIcon(icon);
            tray_ = new QSystemTrayIcon(icon, this);
            auto *menu = new QMenu(this);
            auto *show = menu->addAction(QStringLiteral("Otevřít gpt2codex"));
            connect(show, &QAction::triggered, this, [this] { restore(); });
            auto *refresh = menu->addAction(QStringLiteral("Obnovit relace"));
            connect(refresh, &QAction::triggered, this, [this] { refreshSessions(); });
            menu->addSeparator();
            auto *quit = menu->addAction(QStringLiteral("Ukončit pouze GUI"));
            connect(quit, &QAction::triggered, this, [this] { quitGui(); });
            tray_->setContextMenu(menu);
            tray_->setToolTip(QStringLiteral("gpt2codex – běžící relace Codex"));
            connect(tray_, &QSystemTrayIcon::activated, this, [this](QSystemTrayIcon::ActivationReason reason) {
                if (reason == QSystemTrayIcon::Trigger || reason == QSystemTrayIcon::DoubleClick) {
                    if (isVisible()) { hide(); } else { restore(); }
                }
            });
            tray_->show();
            qApp->setQuitOnLastWindowClosed(false);
        } else {
            statusBar()->showMessage(QStringLiteral("System tray není dostupná; zavření GUI nezastaví screen."));
        }
        const QString workerLogDir = options_.queueRepoRoot.isEmpty()
            ? QString() : QDir(options_.queueRepoRoot).filePath(QStringLiteral(".log/github-queue"));
        workerPage_ = new g2cworker::QueueWorkerPage(workerLogDir, tabs_,
#ifdef G2C_GUI_TEST
            false
#else
            true
#endif
        );
        tabs_->addTab(workerPage_, QIcon::fromTheme("system-run"), QStringLiteral("Queue Worker"));
        tabs_->tabBar()->setTabButton(0, QTabBar::LeftSide, nullptr);
        tabs_->tabBar()->setTabButton(0, QTabBar::RightSide, nullptr);
        const int queueIndex = tabs_->addTab(queuePage_, QStringLiteral("GitHub Queue"));
        tabs_->tabBar()->setTabButton(queueIndex, QTabBar::LeftSide, nullptr);
        tabs_->tabBar()->setTabButton(queueIndex, QTabBar::RightSide, nullptr);
        tabs_->setCurrentIndex(0);
        refreshTimer_.setInterval(6000);
        connect(&refreshTimer_, &QTimer::timeout, this, [this] { refreshSessions(); });
        refreshTimer_.start();
        QTimer::singleShot(0, this, [this] { refreshSessions(); });
        queueActivityTimer_.setInterval(3500);
        connect(&queueActivityTimer_, &QTimer::timeout, this, [this] { refreshQueueActivity(); });
        queueActivityTimer_.start();
        QTimer::singleShot(0, this, [this] { refreshQueueActivity(); });
        attentionTimer_.setInterval(1000);
        connect(&attentionTimer_, &QTimer::timeout, this, [this] {
            for (const QString &slot : attention_.expire(attentionClock_.elapsed())) {
                if (auto *page = qobject_cast<SessionPage *>(pages_.value(slot))) { presentPage(page); }
            }
        });
        attentionTimer_.start();
    }

    void showQueueTasks() { tabs_->setCurrentWidget(queuePage_); }

    ~MainWindow() override {
        // A GUI close must not orphan a hanging `screen -ls` grandchild.
        if (query_ && query_->state() != QProcess::NotRunning) {
            killSessionListQuery(query_);
            query_->waitForFinished(1000);
        }
        if (queueActivityQuery_) {
            auto *proc = queueActivityQuery_;
            queueActivityQuery_ = nullptr;
            disconnect(proc, nullptr, this, nullptr);
            if (proc->state() != QProcess::NotRunning) {
                proc->kill();
                proc->waitForFinished(1000);
            }
        }
        attentionSource_->setBindingsChanged({});
        for (auto it = pages_.cbegin(); it != pages_.cend(); ++it) { attentionSource_->unwatch(attention_.ticket(it.key())); }
    }

#ifdef G2C_GUI_TEST
    g2cattention::Ticket attentionTicket(const QString &slot) const { return attention_.ticket(slot); }
    g2cattention::View attentionView(const QString &slot) const { return attention_.view(slot); }
#endif

protected:
    void changeEvent(QEvent *event) override {
        QMainWindow::changeEvent(event);
        if (event->type() == QEvent::PaletteChange || event->type() == QEvent::StyleChange
            || event->type() == QEvent::ApplicationPaletteChange) {
            tabPresentation_.invalidateIcons();
            updateTabPresentations();
        }
    }
    void closeEvent(QCloseEvent *event) override {
        if (!exiting_ && trayAvailable_) {
            hide();
            event->ignore();
            return;
        }
        QMainWindow::closeEvent(event);
    }

private:
    void removeQueueActivityPage(int number) {
        auto *page = queueActivityPages_.take(number);
        if (!page) { return; }
        const int index = tabs_->indexOf(page);
        if (index >= 0) { tabs_->removeTab(index); }
        page->deleteLater(); // This is only a read-only GUI widget.
    }

    void applyQueueActivity(const QByteArray &raw) {
        const QJsonDocument doc = QJsonDocument::fromJson(raw);
        if (!doc.isArray()) { return; }  // Keep existing views on transient read errors.
        QSet<int> visible;
        QList<QJsonObject> rows;
        for (const QJsonValue &value : doc.array()) {
            if (!value.isObject()) { return; }
            const QJsonObject obj = value.toObject();
            const int number = obj.value("number").toInt(-1);
            const QString project = obj.value("project").toString();
            if (number <= 0 || !QRegularExpression(QStringLiteral("^[a-zA-Z0-9_-]{1,64}$"))
                    .match(project).hasMatch() || obj.value("queue_status").toString() != QStringLiteral("running")
                || visible.contains(number)) { return; }
            visible.insert(number);
            rows.append(obj);
        }
        const QList<int> known = queueActivityPages_.keys();
        for (int number : known) {
            if (!visible.contains(number)) { removeQueueActivityPage(number); }
        }
        for (const QJsonObject &obj : rows) {
            const int number = obj.value("number").toInt();
            if (hiddenQueueActivity_.contains(number)) { continue; }
            auto *page = queueActivityPages_.value(number, nullptr);
            if (!page) {
                page = new QueueActivityPage(number, tabs_);
                queueActivityPages_.insert(number, page);
                const int queueIndex = tabs_->indexOf(queuePage_);
                const int index = tabs_->insertTab(queueIndex < 0 ? tabs_->count() : queueIndex,
                    page, obj.value("project").toString());
                tabs_->setTabIcon(index, QIcon::fromTheme(QStringLiteral("system-run")));
            }
            page->update(obj);
            const int index = tabs_->indexOf(page);
            if (index >= 0) {
                tabs_->setTabText(index, page->project());
                tabs_->setTabToolTip(index, QStringLiteral("#%1 · %2").arg(number).arg(page->title()));
            }
        }
    }

    void refreshQueueActivity() {
        if (queueActivityQuery_ || options_.queueHelper.isEmpty()
            || !options_.queueRootError.isEmpty()) { return; }
        const QString python = QStandardPaths::findExecutable(QStringLiteral("python3"));
        const QString script = QFileInfo(options_.queueHelper).dir()
            .filePath(QStringLiteral("gpt2codex-github-queue.py"));
        if (python.isEmpty() || !QFileInfo(script).isFile()) { return; }
        auto *proc = new QProcess(this);
        queueActivityQuery_ = proc;
        proc->setProcessChannelMode(QProcess::SeparateChannels);
        connect(proc, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
            [this, proc](int code, QProcess::ExitStatus status) {
                if (queueActivityQuery_ != proc) { return; }
                queueActivityQuery_ = nullptr;
                const QByteArray payload = proc->readAllStandardOutput();
                proc->deleteLater();
                if (code == 0 && status == QProcess::NormalExit && payload.size() <= 256 * 1024) {
                    applyQueueActivity(payload);
                }
            });
        connect(proc, &QProcess::errorOccurred, this, [this, proc](QProcess::ProcessError) {
            if (queueActivityQuery_ != proc) { return; }
            queueActivityQuery_ = nullptr;
            proc->deleteLater();
        });
        QStringList args{QStringLiteral("-B"), script};
        if (!options_.queueConfig.isEmpty()) {
            args << QStringLiteral("--config") << options_.queueConfig;
        }
        if (!options_.queueRepoRoot.isEmpty()) {
            args << QStringLiteral("--repo-root") << options_.queueRepoRoot;
        }
        args << QStringLiteral("gui-activity");
        proc->start(python, args);
        QTimer::singleShot(18000, proc, [tracked = QPointer<QProcess>(proc)] {
            if (tracked && tracked->state() != QProcess::NotRunning) { tracked->kill(); }
        });
    }

    // These callbacks have presentation authority only. They cannot attach,
    // detach, refresh session discovery, launch a program or answer approvals.
    void presentPage(SessionPage *page) {
        tabPresentation_.apply(tabs_, page, page->session().id, page->session().multiplexer,
            page->tabLabel(maxTabLabelChars_), page->session().task, attention_.view(page->session().key()));
    }

    void syncAttention(SessionPage *page) {
        const auto identity = attentionIdentity(page->session());
        const auto previous = attention_.ticket(identity.slot());
        const auto current = attention_.observe(identity, attentionSource_->binding(identity), page->attentionEnabled());
        if (!(previous == current)) {
            attentionSource_->unwatch(previous);
            if (current.valid()) {
                attentionSource_->watch(current, [window = QPointer<MainWindow>(this), guard = QPointer<SessionPage>(page)](const g2cattention::Event &event) {
                    if (!window || !guard || window->pages_.value(event.ticket.identity.slot()) != guard) { return; }
                    if (window->attention_.apply(event, window->attentionClock_.elapsed())) { window->presentPage(guard); }
                });
            }
        }
        presentPage(page);
    }

    void updateTabPresentations() {
        for (auto it = pages_.cbegin(); it != pages_.cend(); ++it) {
            auto *page = static_cast<SessionPage *>(it.value());
            syncAttention(page);
        }
    }

    // Presentation discovery is independent from session polling/attachment.
    // A failed registry lookup only removes custom icons/unverified paths.
    void refreshProjectRoots() {
        if (rootsQuery_) { return; }
        auto *proc = new QProcess(this);
        rootsQuery_ = proc;
        auto data = std::make_shared<QByteArray>();
        auto failed = std::make_shared<bool>(false);
        auto finish = [this, proc, data, failed](bool ok) {
            if (rootsQuery_ != proc) { return; }
            rootsQuery_ = nullptr;
            data->append(proc->readAllStandardOutput());
            QHash<QString, QString> roots;
            if (!ok || *failed || !g2ctabs::parseProjectRoots(*data, roots)) { roots.clear(); }
            tabPresentation_.setRoots(roots);
            // Worker logs belong to the Queue checkout selected from the Queue
            // config, not an unrelated Rust session/project discovery result.
            updateTabPresentations();
            proc->deleteLater();
        };
        connect(proc, &QProcess::readyReadStandardOutput, this, [proc, data, failed] {
            const QByteArray chunk = proc->readAllStandardOutput();
            if (data->size() + chunk.size() > 65536) { *failed = true; proc->kill(); }
            else if (!*failed) { data->append(chunk); }
        });
        connect(proc, &QProcess::readyReadStandardError, this, [proc] { proc->readAllStandardError(); });
        connect(proc, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
            [finish](int code, QProcess::ExitStatus status) { finish(code == 0 && status == QProcess::NormalExit); });
        connect(proc, &QProcess::errorOccurred, this, [finish](QProcess::ProcessError) { finish(false); });
        QStringList args{QStringLiteral("projects"), QStringLiteral("list")};
        if (!options_.config.isEmpty()) { args << QStringLiteral("--config") << options_.config; }
        proc->start(options_.bridgeCli, args);
        QTimer::singleShot(3000, proc, [this, proc, failed] {
            if (rootsQuery_ == proc) { *failed = true; proc->kill(); }
        });
    }

    void showSettingsTab() {
        if (settingsPage_) {
            tabs_->setCurrentWidget(settingsPage_);
            return;
        }
        auto *page = new SettingsPage(options_, backends_, backendPreference_, maxTabLabelChars_, tabs_);
        settingsPage_ = page;
        page->setApplyCallback([this](const QString &backend, int maxLen) {
            return applySettings(backend, maxLen);
        });
        const int index = tabs_->addTab(page, QIcon::fromTheme(QStringLiteral("configure")),
            QStringLiteral("Nastavení"));
        tabs_->setTabToolTip(index, QStringLiteral("Nastavení aplikace"));
        tabs_->setCurrentIndex(index);
    }

    bool applySettings(const QString &newBackend, int maxLen) {
        if (maxLen < minTabLabelChars || maxLen > maxTabLabelChars
            || backends_.resolve(newBackend).isEmpty()) {
            QMessageBox::warning(this, QStringLiteral("Nastavení"),
                QStringLiteral("Vybraný terminálový backend není k dispozici."));
            return false;
        }
        QSettings settings = guiSettings();
        settings.setValue(QStringLiteral("terminal/backend"), newBackend);
        settings.setValue(QStringLiteral("tabs/maxLabelChars"), maxLen);
        settings.sync();
        if (settings.status() != QSettings::NoError) {
            QMessageBox::warning(this, QStringLiteral("Nastavení"),
                QStringLiteral("Konfiguraci GUI se nepodařilo uložit."));
            return false;
        }
        maxTabLabelChars_ = maxLen;
        for (auto it = pages_.cbegin(); it != pages_.cend(); ++it) {
            auto *page = static_cast<SessionPage *>(it.value());
            const int index = tabs_->indexOf(page);
            if (index >= 0) {
                tabs_->setTabText(index, page->tabLabel(maxTabLabelChars_));
            }
        }
        if (backendPreference_ != newBackend) {
            // Cancel an in-flight discovery first; stale results must not
            // reattach terminals while switching to the new backend.
            if (query_) {
                auto *old = query_;
                query_ = nullptr;
                disconnect(old, nullptr, this, nullptr);
                old->kill();
                old->deleteLater();
            }
            backendPreference_ = newBackend;
            for (auto *widget : pages_) {
                static_cast<SessionPage *>(widget)->prepareBackendChange(newBackend);
            }
            // Give the old screen attach PTYs a moment to detach, then
            // demand a fresh read-only bridge listing before reconnecting.
            QTimer::singleShot(300, this, [this] { refreshSessions(); });
            statusBar()->showMessage(QStringLiteral("Backend změněn. Ověřuji a obnovuji terminálové relace…"), 8000);
        } else {
            statusBar()->showMessage(QStringLiteral("Nastavení uloženo."), 5000);
        }
        return true;
    }

    void addPage(const Session &session) {
        auto *page = new SessionPage(options_, backends_, backendPreference_, session, tabs_);
        page->setRefreshRequested([this] { refreshSessions(); });
        pages_.insert(session.key(), page);
        const int queueIndex = tabs_->indexOf(queuePage_);
        const int index = tabs_->insertTab(queueIndex < 0 ? tabs_->count() : queueIndex,
            page, displayName(session, maxTabLabelChars_));
        page->setPresentationChanged([this, page] { syncAttention(page); });
        syncAttention(page);
        if (session.id == QStringLiteral("bridge")) {
            tabs_->tabBar()->setTabButton(index, QTabBar::LeftSide, nullptr);
            tabs_->tabBar()->setTabButton(index, QTabBar::RightSide, nullptr);
        }
    }

    void removeSessionPage(const QString &key) {
        auto it = pages_.find(key);
        if (it == pages_.end()) { return; }
        auto *page = static_cast<SessionPage *>(it.value());
        attentionSource_->unwatch(attention_.ticket(key));
        attention_.remove(key);
        const int index = tabs_->indexOf(page);
        if (index >= 0) { tabs_->removeTab(index); }
        pages_.erase(it);
        hidden_.remove(key);
        page->deleteLater(); // Disconnects only the transient client; never signals the screen server.
    }

    void refreshSessions() {
        refreshProjectRoots();
        if (query_) { return; }
        QSet<QString> allowed;
        QString allowError;
        const bool allowOk = queueAllowedProjects(options_.queueConfig.isEmpty()
            ? defaultQueueConfig() : options_.queueConfig, allowed, allowError);
        allowlistProblem_->setText(allowError);
        allowlistProblem_->setVisible(!allowOk);
        auto *proc = new QProcess(this);
        query_ = proc;
        // Keep CLI and its `screen -ls` child in a dedicated disposable group.
        proc->setChildProcessModifier([] {
            if (::setpgid(0, 0) != 0) { ::_exit(127); }
        });
        connect(proc, &QProcess::started, proc, [proc] {
            const auto pid = static_cast<pid_t>(proc->processId());
            if (pid > 0 && ::getpgid(pid) == pid) {
                proc->setProperty("_g2cListGroup", static_cast<qint64>(pid));
            }
        });
        proc->setProcessChannelMode(QProcess::SeparateChannels);
        connect(proc, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
            [this, proc, allowed, allowOk](int code, QProcess::ExitStatus exitStatus) {
                // Clean up descendants even when the CLI exits before its child.
                killSessionListQuery(proc);
                if (query_ != proc) { return; }
                query_ = nullptr;
                const QByteArray data = proc->readAllStandardOutput();
                const QString reason = QString::fromUtf8(proc->readAllStandardError()).left(120);
                proc->deleteLater();
                if (code != 0 || exitStatus != QProcess::NormalExit) {
                    statusBar()->showMessage(QStringLiteral("Bridge session list selhal: %1").arg(reason), 9000);
                    return;
                }
                QList<Session> sessions;
                QString error;
                if (!parseSessions(data, sessions, error)) {
                    statusBar()->showMessage(error, 9000);
                    return;
                }
                int running = 0;
                QSet<QString> seenActive;
                for (const Session &session : sessions) {
                    // Bridge's old interactive tab was replaced by the pinned Queue Worker monitor.
                    if (session.id == QStringLiteral("bridge")) { continue; }
                    const QString key = session.key();
                    const bool activeCodex = session.hasCodexView();
                    const bool permitted = allowOk && allowed.contains(session.id);
                    if (!activeCodex) {
                        removeSessionPage(key);
                        continue;
                    }
                    if (!permitted) {
                        if (allowOk) {
                            removeSessionPage(key);
                        } else if (pages_.contains(key)) {
                            // Keep the count aligned with verified views retained while the allowlist is unavailable.
                            ++running;
                        }
                        continue; // On allowlist read failure, preserve an existing verified view only.
                    }
                    ++running;
                    seenActive.insert(key);
                    if (hidden_.contains(key)) { continue; }
                    if (!pages_.contains(key)) {
                        addPage(session);
                    } else {
                        auto *page = static_cast<SessionPage *>(pages_.value(key));
                        page->update(session);
                        const int index = tabs_->indexOf(page);
                        if (index >= 0) { syncAttention(page); }
                    }
                }
                // A malformed/omitted row must not leave an obsolete project tab visible.
                if (allowOk) {
                    const auto keys = pages_.keys();
                    for (const QString &key : keys) {
                        if (!seenActive.contains(key)) { removeSessionPage(key); }
                    }
                }
                statusBar()->showMessage(QStringLiteral("Ověřené Codex relace: %1 · anchor-only a neaktivní projekty jsou skryté. CLI: %2 · config: %3")
                    .arg(running).arg(options_.bridgeCli, options_.config.isEmpty() ? defaultBridgeConfig() : options_.config));
            });
        connect(proc, &QProcess::errorOccurred, this, [this, proc](QProcess::ProcessError) {
            if (query_ != proc) { return; }
            query_ = nullptr;
            statusBar()->showMessage(QStringLiteral("Bridge CLI se nepodařilo spustit."), 9000);
            proc->deleteLater();
        });
        proc->start(options_.bridgeCli, listArguments(options_));
        QTimer::singleShot(8000, this, [this, tracked = QPointer<QProcess>(proc)] {
            if (tracked && query_ == tracked) {
                killSessionListQuery(tracked);
                statusBar()->showMessage(QStringLiteral("Čtení stavu bridge překročilo časový limit."), 9000);
            }
        });
    }

    void restore() {
        showNormal();
        raise();
        activateWindow();
        if (auto *page = qobject_cast<SessionPage *>(tabs_->currentWidget())) {
            QTimer::singleShot(0, page, [page] { page->focusTerminal(); });
        }
        refreshSessions();
    }

    void quitGui() {
        exiting_ = true;
        if (tray_) { tray_->hide(); }
        close(); // Terminal attachments exit; screen/Codex processes are independent.
        qApp->quit();
    }

    Options options_;
    BackendRegistry backends_;
    QString backendPreference_ = readBackendPreference();
    int maxTabLabelChars_ = readMaxTabLabelChars();
    QTabWidget *tabs_ = nullptr;
    QAction *pasteAction_ = nullptr;
    SettingsPage *settingsPage_ = nullptr;
    QueuePage *queuePage_ = nullptr;
    g2cworker::QueueWorkerPage *workerPage_ = nullptr;
    QLabel *allowlistProblem_ = nullptr;
    QHash<QString, QWidget *> pages_;
    QSet<QString> hidden_;
    QHash<int, QueueActivityPage *> queueActivityPages_;
    QSet<int> hiddenQueueActivity_;
    QTimer queueActivityTimer_;
    QProcess *queueActivityQuery_ = nullptr;
    QSystemTrayIcon *tray_ = nullptr;
    QTimer refreshTimer_;
    QProcess *query_ = nullptr;
    QProcess *rootsQuery_ = nullptr;
    g2ctabs::Presentation tabPresentation_;
    g2cattention::MarkerAttentionSource markerAttention_;
    g2cattention::AttentionSource *attentionSource_;
    g2cattention::Manager attention_;
    QElapsedTimer attentionClock_;
    QTimer attentionTimer_;
    bool exiting_ = false;
    bool trayAvailable_ = false;
};

int selfTest(const Options &options) {
    // No terminal widgets or attached processes are ever started here.
    QByteArray sample = "project_id=bridge screen_name=g2c-bridge state=detached session_id=123.g2c-bridge task=Bridge\n"
                        "project_id=k2-smb screen_name=g2c-k2-smb state=attached session_id=456.g2c-k2-smb task=Validace\n";
    QList<Session> sampleSessions;
    QString error;
    if (!parseSessions(sample, sampleSessions, error) || sampleSessions.size() != 2) { return 2; }
    sample += "project_id=evil screen_name=g2c-other state=detached session_id=777.g2c-other task=Injection\n";
    sampleSessions.clear();
    if (parseSessions(sample, sampleSessions, error)) { return 3; }
    // Replacement of a registered PID must never be treated as the same
    // attachment just because the project slot and screen name match.
    const Session original{QStringLiteral("k2-smb"), QStringLiteral("g2c-k2-smb"),
        QStringLiteral("detached"), QStringLiteral("456.g2c-k2-smb"), QStringLiteral("Task")};
    Session replacement = original;
    replacement.sessionId = QStringLiteral("789.g2c-k2-smb");
    if (!sameLiveSessionIdentity(original, original)
        || sameLiveSessionIdentity(original, replacement)) { return 11; }
    replacement = original;
    replacement.state = QStringLiteral("unconfirmed");
    if (sameLiveSessionIdentity(original, replacement)) { return 12; }
    replacement = original;
    replacement.screenName = QStringLiteral("g2c-unrelated");
    if (sameLiveSessionIdentity(original, replacement)) { return 13; }
    Session longTask{QStringLiteral("k2-smb"), QStringLiteral("g2c-k2-smb"),
        QStringLiteral("detached"), QStringLiteral("456.g2c-k2-smb"), QString(180, QLatin1Char('X'))};
    if (displayName(longTask).size() != 32 || displayName(longTask, 16).size() != 16
        || displayName(longTask, 128).size() != 128
        || !displayName(longTask, 16).endsWith(QStringLiteral("… [screen]"))
        || displayName(Session{QStringLiteral("bridge"), {}, {}, {}, {}}, 12) != QStringLiteral("Bridge [screen]")) {
        return 7;
    }
    // Headless image checks cover native Tile repetition, NoScaling anchor,
    // and full-viewport Stretch. No PTY or screen session is touched.
    QImage sampleImage(2, 2, QImage::Format_ARGB32);
    sampleImage.setPixelColor(0, 0, Qt::red);
    sampleImage.setPixelColor(1, 0, Qt::blue);
    sampleImage.setPixelColor(0, 1, Qt::green);
    sampleImage.setPixelColor(1, 1, Qt::yellow);
    TerminalAppearance wallpaperTest;
    const QPixmap pixmap = QPixmap::fromImage(sampleImage);
    wallpaperTest.wallpaperFillStyle = QStringLiteral("Tile");
    const QImage tiled = renderKonsoleWallpaper(pixmap, QSize(5, 4), wallpaperTest);
    if (tiled.isNull() || tiled.pixelColor(2, 0) != Qt::red || tiled.pixelColor(4, 3) != Qt::green) {
        return 8;
    }
    wallpaperTest.wallpaperFillStyle = QStringLiteral("NoScaling");
    wallpaperTest.wallpaperAnchor = QPointF(1.0, 1.0);
    const QImage anchored = renderKonsoleWallpaper(pixmap, QSize(5, 4), wallpaperTest);
    if (anchored.isNull() || anchored.pixelColor(3, 2) != Qt::red || anchored.pixelColor(0, 0).alpha() != 0) {
        return 9;
    }
    wallpaperTest.wallpaperFillStyle = QStringLiteral("Stretch");
    const QImage stretched = renderKonsoleWallpaper(pixmap, QSize(5, 4), wallpaperTest);
    if (stretched.isNull() || stretched.pixelColor(4, 3).alpha() != 255) { return 10; }
    const QByteArray safeDiagnostic =
        "P0J=OK project_id=k2-smb protocol=stdio metadata=readonly state_db_only=1\n"
        "P0J_SESSIONS=count:1 active:0 idle:0 not_loaded:1 system_error:0 more:0\n"
        "P0J_EVENTS=thread:1 turn:0 item:0 other:0 scope=diagnostic_connection_only\n"
        "P0J_TUI=separate_process live_attachment=not_verified control=disabled\n";
    if (!validCodexDiagnostics(safeDiagnostic)
        || validCodexDiagnostics(safeDiagnostic + "SENSITIVE EXTRA DATA\n")
        || validCodexDiagnostics("P0J=OK project_id=k2-smb;evil protocol=stdio metadata=readonly state_db_only=1\n")) {
        return 14;
    }
    QProcess proc;
    proc.start(options.bridgeCli, listArguments(options));
    if (!proc.waitForStarted(2000) || !proc.waitForFinished(5000)) {
        proc.kill();
        proc.waitForFinished(1000);
        std::fprintf(stderr, "P0IC_GUI=FAIL reason=bridge_timeout\n");
        return 4;
    }
    if (proc.exitStatus() != QProcess::NormalExit || proc.exitCode() != 0) {
        std::fprintf(stderr, "P0IC_GUI=FAIL reason=bridge_status\n");
        return 5;
    }
    QList<Session> sessions;
    if (!parseSessions(proc.readAllStandardOutput(), sessions, error)) {
        std::fprintf(stderr, "P0IC_GUI=FAIL reason=bridge_output\n");
        return 6;
    }
    int active = 0;
    for (const Session &session : sessions) { if (session.running()) { ++active; } }
    std::printf("P0J_GUI=OK page=codex_api parser=strict attached=0 started=0\n");
    std::printf("P0IF_IDENTITY=OK same=accepted replacement=blocked unavailable=blocked attached=0 started=0\n");
    std::printf("P0IC_GUI=OK sessions=%lld active=%d attached=0 started=0\n", static_cast<long long>(sessions.size()), active);
    return 0;
}

} // namespace
#include "main.moc"

#ifdef G2C_GUI_TEST
#include "lifecycle_tests.h"
#include "moc_lifecycle_tests.cpp"
#else
int main(int argc, char *argv[]) {
    QApplication app(argc, argv);
    QCoreApplication::setApplicationName(QStringLiteral("gpt2codex-gui"));
    Options options;
    QString error;
    if (!parseOptions(app.arguments(), options, error)) {
        std::fprintf(stderr, "gpt2codex-gui: %s\n", error.toUtf8().constData());
        std::fprintf(stderr, "Použití: gpt2codex-gui [--bridge-cli /absolute/path/gpt2codex] [--config /absolute/config.toml] [--self-test] [--theme-probe] [--settings-probe] [--backend-probe] [--queue-helper /absolute/helper.py] [--queue-config /absolute/queue-config.json] [--queue-only]\n");
        return 2;
    }
    if (options.selfTest) { return selfTest(options); }
    if (options.settingsProbe || options.backendProbe) {
        const QSettings settings = guiSettings();
        const QString selection = readBackendPreference();
        const BackendRegistry registry;
        const QByteArray effective = registry.resolve(selection).toUtf8();
        const QByteArray configured = selection.toUtf8();
        if (options.settingsProbe) {
            // Keep config= last for existing shell helpers that parse the path.
            std::printf("P0IC_SETTINGS=OK max_tab_label_chars=%d backend=%s effective=%s config=%s\n",
                readMaxTabLabelChars(), configured.constData(), effective.constData(),
                settings.fileName().toUtf8().constData());
        } else {
            std::printf("P0IE_BACKEND=OK configured=%s effective=%s konsole=%d qtermwidget=%d attached=0 started=0\n",
                configured.constData(), effective.constData(),
                registry.available(QString::fromLatin1(backendKonsole)) ? 1 : 0,
                registry.available(QString::fromLatin1(backendQTerm)) ? 1 : 0);
        }
        return 0;
    }
    if (options.themeProbe) {
        const TerminalAppearance theme = terminalAppearance();
        const QByteArray source = theme.source.toUtf8();
        const QByteArray name = QFileInfo(theme.scheme).fileName().toUtf8();
        const QByteArray uiStyle = app.style()->objectName().toUtf8();
        const QByteArray family = theme.font.family().toUtf8();
        const QByteArray wallpaper = theme.wallpaperStatus.toUtf8();
        const QByteArray opacity = QString::number(theme.opacity, 'f', 2).toUtf8();
        const QByteArray background = theme.backgroundColor.toUtf8();
        const QByteArray fill = theme.wallpaperFillStyle.toUtf8();
        const QByteArray flip = theme.wallpaperFlip.toUtf8();
        std::printf("P0IC_THEME=OK ui_style=%s terminal_source=%s terminal_scheme=%s terminal_font=%s background_opacity=%s wallpaper=%s background_color=%s wallpaper_fill=%s wallpaper_anchor=%.2f,%.2f wallpaper_opacity=%.2f wallpaper_flip=%s\n",
            uiStyle.constData(), source.constData(), name.constData(), family.constData(),
            opacity.constData(), wallpaper.constData(), background.constData(), fill.constData(),
            theme.wallpaperAnchor.x(), theme.wallpaperAnchor.y(), theme.wallpaperOpacity, flip.constData());
        return 0;
    }
    MainWindow mainWindow(std::move(options));
    mainWindow.show();
    return app.exec();
}
#endif
