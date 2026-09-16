"""Telegram: envio em pedaços e à prova de erro HTTP (um /STATUS longo não pode derrubar o LIVE)."""
import unittest



class TelegramRobustnessTests(unittest.TestCase):
    def test_chunks_split_on_newlines_under_limit(self):
        from gold_ai.telegram import TelegramSender
        text = "\n".join(f"linha {i} " + "x" * 80 for i in range(120))      # ≈ 10 000 caracteres
        parts = TelegramSender.chunks(text, 3900)
        self.assertGreaterEqual(len(parts), 3)
        self.assertTrue(all(len(p) <= 3900 for p in parts))
        self.assertEqual("\n".join(parts), text)                              # nada perdido
        self.assertEqual(TelegramSender.chunks("curto", 3900), ["curto"])
        huge = "y" * 9000
        self.assertTrue(all(len(p) <= 3900 for p in TelegramSender.chunks(huge, 3900)))

    def test_send_never_raises_on_http_error(self):
        import io
        import urllib.error
        import urllib.request
        from gold_ai.telegram import TelegramSender
        s = TelegramSender(token="t", chat_id="c", dry_run=False)
        orig = urllib.request.urlopen

        def boom(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"ok":false,"description":"message is too long"}'))
        urllib.request.urlopen = boom
        try:
            self.assertFalse(s.send("x" * 5000))                               # duas mensagens, ambas falham, nenhuma exceção
        finally:
            urllib.request.urlopen = orig
