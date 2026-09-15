"""DUKASCOPY — ticks bid/ask históricos, gratuitos e sem chave (fonte alternativa ao MT5 para o REACTION ENGINE).

URL por hora: https://datafeed.dukascopy.com/datafeed/<INSTRUMENTO>/<ANO>/<MÊS-1 com 2 dígitos>/<DIA>/<HORA>h_ticks.bi5
Cada arquivo é LZMA com registros big-endian de 20 bytes: (ms desde o início da hora: uint32, ask: uint32, bid: uint32,
volume ask: float32, volume bid: float32). Preços inteiros divididos pela escala do instrumento (confira o primeiro tick).
Hora sem dados (fim de semana, feriado) = 404 → vazio. Meses no caminho começam em 00 (janeiro).
"""

from __future__ import annotations

import lzma
import struct
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

DUKA_BASE = "https://datafeed.dukascopy.com/datafeed"

# mercado do MARKET AI → (instrumento Dukascopy, escala de preço)
DUKA_INSTRUMENTS: dict[str, tuple[str, float]] = {
    "XAUUSD": ("XAUUSD", 1000.0),
    "EURUSD": ("EURUSD", 100000.0),
    "GBPUSD": ("GBPUSD", 100000.0),
    "USDJPY": ("USDJPY", 1000.0),
    "US500": ("USA500IDXUSD", 1000.0),
    "NAS100": ("USATECHIDXUSD", 1000.0),
    "WTI": ("LIGHTCMDUSD", 1000.0),
    "USDX": ("DOLLARIDXUSD", 1000.0),      # índice do dólar: líder USD
    "US10Y": ("USTBONDTRUSD", 1000.0),     # T-Bond futuro (proxy inverso de yields; use --lead-yield com cautela)
    "BTCUSD": ("BTCUSD", 10.0),
}


def parse_bi5(data: bytes, hour_start: datetime, scale: float) -> list[tuple[datetime, float, float]]:
    if not data:
        return []
    raw = lzma.decompress(data)
    out = []
    for off in range(0, len(raw) - len(raw) % 20, 20):
        ms, ask, bid, _va, _vb = struct.unpack(">IIIff", raw[off:off + 20])
        if bid <= 0 or ask <= 0:
            continue
        out.append((hour_start + timedelta(milliseconds=ms), bid / scale, ask / scale))
    return out


def hour_url(instrument: str, t: datetime) -> str:
    return f"{DUKA_BASE}/{instrument}/{t.year}/{t.month - 1:02d}/{t.day:02d}/{t.hour:02d}h_ticks.bi5"


class DukascopyImporter:
    """Uma hora que falhe (timeout, 503) é tentada de novo com espera crescente; se persistir, é anotada em `failed` e pulada —
    o download continua. Horas baixadas ficam em cache (HttpClient), então repetir o comando refaz só o que faltou."""

    def __init__(self, http, log: Optional[Callable[[str], None]] = None, retries: int = 4, sleep=None, pace: float = 0.15) -> None:
        import time as _time
        self.http, self._log, self.retries, self.pace = http, log, retries, pace
        self._sleep = sleep or _time.sleep
        self.failed: list[tuple[str, datetime, str]] = []

    def _fetch_hour(self, inst: str, h: datetime) -> Optional[bytes]:
        from .http import DataError
        for attempt in range(self.retries + 1):
            try:
                recent = h > datetime.now(timezone.utc) - timedelta(hours=48)
                data = self.http.get_bytes(hour_url(inst, h), ttl=(3600 if recent else 365 * 24 * 3600), allow_404=True)   # 404 recente não é 'sem dados' para sempre
                if self.pace:
                    self._sleep(self.pace)
                return data
            except (DataError, OSError, EOFError) as e:
                if attempt == self.retries:
                    self.failed.append((inst, h, str(e)[-80:]))
                    if self._log:
                        self._log(f"  {inst} {h:%Y-%m-%d %H}h: FALHOU após {self.retries + 1} tentativas — pulada (repita o comando para completar)")
                    return None
                self._sleep(min(60.0, 3.0 * (2 ** attempt)))
        return None

    def hours(self, market: str, hours: Sequence[datetime], scale: Optional[float] = None) -> list[tuple[datetime, float, float]]:
        inst, sc = DUKA_INSTRUMENTS.get(market.upper(), (market.upper(), 1000.0))
        sc = scale or sc
        out = []
        for h in sorted(set(x.replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc) for x in hours)):
            if h.weekday() == 5 or (h.weekday() == 6 and h.hour < 21):
                continue                                              # mercado fechado: nem pede
            data = self._fetch_hour(inst, h)
            if data is None:
                continue
            try:
                out += parse_bi5(data, h, sc)
            except Exception as e:  # noqa: BLE001 — arquivo corrompido/incompleto
                self.failed.append((inst, h, f"bi5 inválido: {e}"))
        out.sort()
        return out

    def around_events(self, market: str, event_times: Sequence[datetime], before_h: int = 4, after_h: int = 1, scale: Optional[float] = None,
                      checkpoint: Optional[Callable[[list], None]] = None) -> list[tuple[datetime, float, float]]:
        """Só as horas ao redor de cada evento (−before_h … +after_h): amostra grande sem baixar o ano inteiro."""
        hours: list[datetime] = []
        for t in event_times:
            t = t.astimezone(timezone.utc)
            for k in range(-before_h, after_h + 1):
                hours.append(t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=k))
        uniq = sorted(set(hours))
        if self._log:
            self._log(f"Dukascopy {market}: {len(uniq)} horas ao redor de {len(event_times)} eventos")
        out = []
        for i in range(0, len(uniq), 24):
            out += self.hours(market, uniq[i:i + 24], scale)
            if checkpoint:
                checkpoint(out)
            if self._log and (i // 24) % 10 == 9:
                self._log(f"  {market}: {min(i + 24, len(uniq))}/{len(uniq)} horas · {len(out)} ticks")
        return out

    def range(self, market: str, start: date, end: date, scale: Optional[float] = None, checkpoint: Optional[Callable[[list], None]] = None) -> list:
        hours = []
        t = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        t_end = datetime(end.year, end.month, end.day, 23, tzinfo=timezone.utc)
        while t <= t_end:
            if t.weekday() < 5 or (t.weekday() == 6 and t.hour >= 21):
                hours.append(t)
            t += timedelta(hours=1)
        if self._log:
            self._log(f"Dukascopy {market}: {len(hours)} horas ({start} → {end})")
        out = []
        for i in range(0, len(hours), 24):
            out += self.hours(market, hours[i:i + 24], scale)
            if checkpoint:
                checkpoint(out)
        return out
