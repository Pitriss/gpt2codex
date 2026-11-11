// Typed source boundary only. P0-A has no live transport, hook or Codex client.
#pragma once
#include "attention_state.h"
#include <functional>

namespace g2cattention {

class AttentionSource {
public:
    using Delivery = std::function<void(const Event &)>;
    virtual ~AttentionSource() = default;
    virtual std::optional<Run> binding(const Identity &) const = 0;
    virtual void watch(const Ticket &, Delivery) = 0;
    virtual void unwatch(const Ticket &) = 0;
    // One source per GUI manager. A newly verified file/run binding can wake
    // presentation immediately, independently of the 6s session poll.
    void setBindingsChanged(std::function<void()> handler) { changed_ = std::move(handler); }
protected:
    void bindingsChanged() { if (changed_) { changed_(); } }
private:
    std::function<void()> changed_;
};

class UnavailableAttentionSource final : public AttentionSource {
public:
    std::optional<Run> binding(const Identity &) const override { return std::nullopt; }
    void watch(const Ticket &, Delivery) override {}
    void unwatch(const Ticket &) override {}
};

#ifdef G2C_GUI_TEST
// Available only in the test binary: no command-line/config switch can enable
// synthetic colors in the real GUI. The fixture author establishes bindings.
class FakeAttentionSource final : public AttentionSource {
public:
    void setBinding(const Identity &identity, const Run &run) {
        bindings_.insert(identity.slot(), {identity, run}); bindingsChanged();
    }
    std::optional<Run> binding(const Identity &identity) const override {
        auto it = bindings_.constFind(identity.slot());
        return it != bindings_.cend() && it->first == identity ? std::optional<Run>(it->second) : std::nullopt;
    }
    void watch(const Ticket &ticket, Delivery delivery) override { watches_.insert(ticket.viewEpoch, std::move(delivery)); }
    void unwatch(const Ticket &ticket) override { watches_.remove(ticket.viewEpoch); }
    void send(const Event &event) const {
        const auto delivery = watches_.value(event.ticket.viewEpoch);
        if (delivery) { delivery(event); }
    }
    // Retaining a copy lets tests deliver an in-flight callback after unwatch.
    Delivery queued(const Ticket &ticket) const { return watches_.value(ticket.viewEpoch); }
    int subscriptions() const { return watches_.size(); }
private:
    QHash<QString, QPair<Identity, Run>> bindings_;
    QHash<QString, Delivery> watches_;
};
#endif

} // namespace g2cattention
