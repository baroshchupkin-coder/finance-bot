import unittest

from callback_safety import CallbackDebouncer


class CallbackDebouncerTests(unittest.TestCase):
    def test_repeated_action_is_blocked_inside_window(self):
        debouncer = CallbackDebouncer(window_seconds=15)

        self.assertTrue(debouncer.claim("approve", "696", now=100))
        self.assertFalse(debouncer.claim("approve", "696", now=101))

    def test_different_scopes_and_requests_are_independent(self):
        debouncer = CallbackDebouncer(window_seconds=15)

        self.assertTrue(debouncer.claim("approval", "696", now=100))
        self.assertTrue(debouncer.claim("payment", "696", now=101))
        self.assertTrue(debouncer.claim("approval", "697", now=101))

    def test_action_can_be_retried_after_window(self):
        debouncer = CallbackDebouncer(window_seconds=15)

        self.assertTrue(debouncer.claim("paid", "696", now=100))
        self.assertTrue(debouncer.claim("paid", "696", now=116))

    def test_released_action_can_be_retried_immediately(self):
        debouncer = CallbackDebouncer(window_seconds=15)

        self.assertTrue(debouncer.claim("approve", "696", now=100))
        debouncer.release("approve", "696")
        self.assertTrue(debouncer.claim("approve", "696", now=101))


if __name__ == "__main__":
    unittest.main()
