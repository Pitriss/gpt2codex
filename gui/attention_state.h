// P0-A: metadata-only, in-memory attention model. No process/terminal operations.
#pragma once

#include <QHash>
#include <QList>
#include <QString>
#include <QStringList>
#include <QUuid>
#include <optional>

namespace g2cattention {

enum class State { Unknown, Working, Waiting, Completed };
enum class Reason { Unavailable, Identity, Suspended, Snapshot, Lost, Evidence, Expired, Idle, Failed, Interrupted, WaitingCleared, None };
enum class Phase { Idle, Running, Completed, Failed, Interrupted };
enum class WaitReason { Approval, Input };
enum class Kind { Snapshot, TurnStarted, Update, Heartbeat, SourceLost, WaitingSnapshot, WaitingChanged };

inline bool identifier(const QString &value, int limit = 128) {
    if (value.isEmpty() || value.size() > limit) { return false; }
    for (const QChar ch : value) {
        const ushort c = ch.unicode();
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')
                || c == '-' || c == '_' || c == '.' || c == ':' || c == '$')) { return false; }
    }
    return value != QStringLiteral("-") && value != QStringLiteral(".") && value != QStringLiteral("..");
}

struct Identity {
    QString project, multiplexer, sessionId, generation;
    QString slot() const { return multiplexer + QLatin1Char(':') + project; }
    bool valid() const {
        for (const QChar c : project) {
            if (!((c >= QLatin1Char('a') && c <= QLatin1Char('z'))
                    || (c >= QLatin1Char('0') && c <= QLatin1Char('9'))
                    || c == QLatin1Char('-') || c == QLatin1Char('_'))) { return false; }
        }
        for (const QChar c : generation) {
            if (c < QLatin1Char('0') || c > QLatin1Char('9')) { return false; }
        }
        bool ok = false;
        const auto ticks = generation.toULongLong(&ok);
        return identifier(project, 64) && (multiplexer == "screen" || multiplexer == "tmux")
            && identifier(sessionId) && ok && ticks > 0;
    }
    bool operator==(const Identity &other) const {
        return project == other.project && multiplexer == other.multiplexer
            && sessionId == other.sessionId && generation == other.generation;
    }
};

// A future provider must establish this binding outside event delivery. A
// screen/tmux server generation alone does not identify the Codex process.
struct Run {
    QString nonce, sourceEpoch, threadId;
    // P0-M can report only ordinary-question waiting, without a Codex thread
    // API or a complete turn stream. Clearing it cannot assert red or green.
    bool waitingOnly = false;
    bool valid() const {
        return identifier(nonce) && identifier(sourceEpoch)
            && (identifier(threadId) || (waitingOnly && threadId.isEmpty()));
    }
    bool operator==(const Run &other) const {
        return nonce == other.nonce && sourceEpoch == other.sourceEpoch
            && threadId == other.threadId && waitingOnly == other.waitingOnly;
    }
};

struct Ticket {
    Identity identity;
    Run run;
    QString viewEpoch; // Random per subscription, including after a GUI restart.
    bool valid() const { return identity.valid() && run.valid() && !viewEpoch.isEmpty(); }
    bool operator==(const Ticket &other) const {
        return identity == other.identity && run == other.run && viewEpoch == other.viewEpoch;
    }
};

struct Wait { QString id; WaitReason reason = WaitReason::Input; };

// Local normalized contract, NOT a Codex RPC. Updates contain the full pending
// set, never incremental guesses. Sequence/turn ordinal require an ordered,
// authoritative provider; assigning an arrival counter is not proof of that.
struct Event {
    Ticket ticket;
    quint64 sequence = 0;
    Kind kind = Kind::Update;
    QString turnId;
    quint64 turnOrdinal = 0;
    Phase phase = Phase::Idle;
    QList<Wait> pending;
    bool complete = false;
    bool waiting = false; // Used only by WaitingSnapshot/WaitingChanged.
};

struct View {
    State state = State::Unknown;
    Reason reason = Reason::Unavailable;
    bool approval = false;
    bool input = false;
};

inline QString description(const View &view) {
    switch (view.state) {
    case State::Working: return QStringLiteral("Pracuje — ověřený tah");
    case State::Waiting:
        return view.approval && view.input ? QStringLiteral("Čeká: schválení a uživatelský vstup")
            : view.approval ? QStringLiteral("Čeká: schválení") : QStringLiteral("Čeká: uživatelský vstup");
    case State::Completed: return QStringLiteral("Tah dokončen — ověřeno");
    case State::Unknown: break;
    }
    switch (view.reason) {
    case Reason::Identity: return QStringLiteral("Neověřeno: identita relace není potvrzena");
    case Reason::Suspended: return QStringLiteral("Neověřeno: sledování pohledu je pozastaveno");
    case Reason::Snapshot: return QStringLiteral("Neověřeno: čeká se na čerstvý stav zdroje");
    case Reason::Lost: return QStringLiteral("Neověřeno: zdroj byl odpojen");
    case Reason::Evidence: return QStringLiteral("Neověřeno: neúplné nebo nesouvislé události");
    case Reason::Expired: return QStringLiteral("Neověřeno: stav zdroje zastaral");
    case Reason::Idle: return QStringLiteral("Neověřeno: bez doloženého dokončení tahu");
    case Reason::Failed: return QStringLiteral("Neověřeno: tah selhal");
    case Reason::Interrupted: return QStringLiteral("Neověřeno: tah byl přerušen");
    case Reason::WaitingCleared: return QStringLiteral("Neověřeno: čekání není hlášeno");
    default: return QStringLiteral("Neověřeno: běh nemá ověřený attention zdroj");
    }
}

class Manager final {
public:
    static constexpr qint64 freshnessMs = 15000;

    Ticket observe(const Identity &identity, const std::optional<Run> &run, bool enabled) {
        auto it = entries_.find(identity.slot());
        if (it != entries_.end() && it->identity == identity && it->run == run && it->enabled == enabled) {
            return it->ticket;
        }
        Entry entry;
        entry.identity = identity; entry.run = run; entry.enabled = enabled;
        if (!identity.valid()) { entry.view.reason = Reason::Identity; }
        else if (!enabled) { entry.view.reason = Reason::Suspended; }
        else if (run && run->valid()) {
            entry.ticket = {identity, *run, QUuid::createUuid().toString(QUuid::WithoutBraces)};
            entry.view.reason = Reason::Snapshot;
        }
        entries_.insert(identity.slot(), entry);
        return entry.ticket;
    }
    Ticket ticket(const QString &slot) const { return entries_.value(slot).ticket; }
    View view(const QString &slot) const { return entries_.value(slot).view; }
    void remove(const QString &slot) { entries_.remove(slot); }

    bool apply(const Event &event, qint64 now) {
        auto it = entries_.find(event.ticket.identity.slot());
        if (it == entries_.end() || !event.ticket.valid() || !(it->ticket == event.ticket)
            || it->blocked || now < 0 || event.sequence == 0 || event.sequence <= it->sequence) { return false; }
        Entry &entry = it.value();
        if (event.kind == Kind::SourceLost) { invalidate(entry, Reason::Lost); return true; }
        const bool snapshot = event.kind == Kind::Snapshot || event.kind == Kind::WaitingSnapshot;
        if ((!entry.sequence && !snapshot)
            || (entry.sequence && event.sequence != entry.sequence + 1)) {
            invalidate(entry, Reason::Evidence); return true;
        }
        // A snapshot answers this ticket's initial barrier only. A late one
        // must not overwrite a newer turn, even if it has a newer sequence.
        if (entry.sequence && snapshot) { return false; }
        entry.sequence = event.sequence;
        if (event.kind == Kind::Heartbeat) {
            entry.received = now; return true; // Only an already synchronized source can heartbeat.
        }
        if (entry.ticket.run.waitingOnly) {
            if (event.kind != Kind::WaitingSnapshot && event.kind != Kind::WaitingChanged) {
                invalidate(entry, Reason::Evidence); return true;
            }
            entry.received = now;
            entry.view = event.waiting ? View{State::Waiting, Reason::None, false, true}
                : View{State::Unknown, Reason::WaitingCleared};
            return true;
        }
        if (!event.complete || !validEvidence(event)) { invalidate(entry, Reason::Evidence); return true; }
        if (event.phase == Phase::Idle && entry.turnOrdinal) { invalidate(entry, Reason::Idle); return true; }
        if (entry.turnOrdinal && event.turnOrdinal < entry.turnOrdinal) { return false; }
        if (entry.turnOrdinal && event.turnOrdinal == entry.turnOrdinal && event.turnId != entry.turnId) {
            invalidate(entry, Reason::Evidence); return true;
        }
        if (event.kind != Kind::Snapshot && event.kind != Kind::TurnStarted && event.kind != Kind::Update) {
            invalidate(entry, Reason::Evidence); return true;
        }
        if (event.kind == Kind::TurnStarted) {
            if (event.turnOrdinal <= entry.turnOrdinal) { return false; }
            if (event.phase != Phase::Running || !event.pending.isEmpty() || event.turnId == entry.turnId) {
                invalidate(entry, Reason::Evidence); return true;
            }
        } else if (event.kind == Kind::Update && event.turnOrdinal != entry.turnOrdinal) {
            invalidate(entry, Reason::Evidence); return true;
        }
        entry.turnId = event.turnId; entry.turnOrdinal = event.turnOrdinal; entry.received = now;
        entry.view = {};
        if (!event.pending.isEmpty()) {
            entry.view.state = State::Waiting; entry.view.reason = Reason::None;
            for (const Wait &wait : event.pending) {
                entry.view.approval |= wait.reason == WaitReason::Approval;
                entry.view.input |= wait.reason == WaitReason::Input;
            }
        } else if (event.phase == Phase::Running) { entry.view = {State::Working, Reason::None}; }
        else if (event.phase == Phase::Completed) { entry.view = {State::Completed, Reason::None}; }
        else { entry.view.reason = event.phase == Phase::Failed ? Reason::Failed
            : event.phase == Phase::Interrupted ? Reason::Interrupted : Reason::Idle; }
        return true;
    }

    QStringList expire(qint64 now) {
        QStringList changed;
        for (auto it = entries_.begin(); it != entries_.end(); ++it) {
            if (!it->blocked && it->sequence && now >= it->received
                && now - it->received >= freshnessMs) {
                invalidate(it.value(), Reason::Expired); changed.append(it.key());
            }
        }
        return changed;
    }

private:
    struct Entry {
        Identity identity;
        std::optional<Run> run;
        Ticket ticket;
        View view;
        bool enabled = false, blocked = false;
        quint64 sequence = 0, turnOrdinal = 0;
        QString turnId;
        qint64 received = 0;
    };
    static void invalidate(Entry &entry, Reason reason) {
        entry.view = {State::Unknown, reason}; entry.blocked = true;
        // Recovery requires a new subscription/binding, never an unsolicited snapshot.
    }
    static bool validEvidence(const Event &event) {
        if (event.pending.size() > 64) { return false; }
        if (event.phase == Phase::Idle) {
            return event.turnId.isEmpty() && event.turnOrdinal == 0 && event.pending.isEmpty();
        }
        if (!identifier(event.turnId) || !event.turnOrdinal) { return false; }
        if (event.phase != Phase::Running && event.phase != Phase::Completed
            && event.phase != Phase::Failed && event.phase != Phase::Interrupted) { return false; }
        QHash<QString, bool> requests;
        for (const Wait &wait : event.pending) {
            if (!identifier(wait.id) || requests.contains(wait.id)
                || (wait.reason != WaitReason::Approval && wait.reason != WaitReason::Input)) { return false; }
            requests.insert(wait.id, true);
        }
        return event.pending.isEmpty() || event.phase == Phase::Running || event.phase == Phase::Completed;
    }
    QHash<QString, Entry> entries_;
};

} // namespace g2cattention
