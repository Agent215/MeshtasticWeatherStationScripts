class BridgeState:
    BOOTING = "BOOTING"
    WIFI_CONNECTING = "WIFI_CONNECTING"
    UDP_READY = "UDP_READY"
    RECOVERING = "RECOVERING"
    FATAL_RESTART_PENDING = "FATAL_RESTART_PENDING"


class StateMachine:
    def __init__(self):
        self.state = BridgeState.BOOTING
        self.transitions = [(None, BridgeState.BOOTING)]

    def set_state(self, new_state, reason=None):
        if new_state != self.state:
            self.transitions.append((self.state, new_state, reason))
            self.state = new_state

    def is_fatal(self):
        return self.state == BridgeState.FATAL_RESTART_PENDING
