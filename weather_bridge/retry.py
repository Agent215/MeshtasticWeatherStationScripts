class BackoffPolicy:
    def __init__(self, base_delay=1.0, factor=2.0, max_delay=30.0):
        self.base_delay = float(base_delay)
        self.factor = float(factor)
        self.max_delay = float(max_delay)
        self.attempt = 0

    def next_delay(self):
        delay = self.base_delay * (self.factor ** self.attempt)
        self.attempt += 1
        if delay > self.max_delay:
            delay = self.max_delay
        return delay

    def reset(self):
        self.attempt = 0
