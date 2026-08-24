from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .cloud import CloudOffer


class ProviderError(RuntimeError):
    pass


class _JSONClient:
    def __init__(self, token: str, *, timeout_seconds: float = 30) -> None:
        if not token:
            raise ValueError("provider API token must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("provider timeout must be positive")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def _request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "PuzzleForge-elastic/0.1",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            raise ProviderError(f"provider HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderError(f"provider request failed: {exc}") from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError("provider returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise ProviderError("provider response must be a JSON object")
        return parsed


class VastClient(_JSONClient):
    ENDPOINT = "https://console.vast.ai/api/v0/bundles/"

    def search_offers(
        self,
        *,
        gpu_models: tuple[str, ...] = (),
        interruptible: bool = True,
        minimum_reliability: float = 0.98,
        limit: int = 100,
    ) -> list[CloudOffer]:
        if not 1 <= limit <= 1_000:
            raise ValueError("Vast offer limit must be in [1, 1000]")
        query: dict[str, Any] = {
            "verified": {"eq": True},
            "rentable": {"eq": True},
            "reliability": {"gte": minimum_reliability},
            "num_gpus": {"gte": 1},
            "direct_port_count": {"gte": 1},
            "order": [["dph", "asc"]],
            "type": "bid" if interruptible else "on-demand",
            "limit": limit,
        }
        if gpu_models:
            query["gpu_name"] = (
                {"eq": gpu_models[0]}
                if len(gpu_models) == 1
                else {"in": list(gpu_models)}
            )
        response = self._request("POST", self.ENDPOINT, query)
        return parse_vast_offers(response, interruptible=interruptible)


class RunPodClient(_JSONClient):
    ENDPOINT = "https://api.runpod.io/v2/catalog/gpus"

    def catalog_offers(
        self,
        *,
        cloud: str = "SECURE",
        minimum_cuda: str = "11.8",
        countries: tuple[str, ...] = (),
    ) -> list[CloudOffer]:
        cloud = cloud.upper()
        if cloud not in {"SECURE", "COMMUNITY"}:
            raise ValueError("RunPod cloud must be SECURE or COMMUNITY")
        parameters = {
            "include": "AVAILABILITY",
            "product": "POD",
            "count": "1",
            "cloud": cloud,
            "minCudaVersion": minimum_cuda,
        }
        if countries:
            parameters["countryCodes"] = ",".join(countries)
        response = self._request(
            "GET", f"{self.ENDPOINT}?{urlencode(parameters)}"
        )
        return parse_runpod_offers(response, cloud=cloud)


def parse_vast_offers(
    payload: dict[str, Any], *, interruptible: bool
) -> list[CloudOffer]:
    raw_offers = payload.get("offers")
    if not isinstance(raw_offers, list):
        raise ProviderError("Vast response is missing offers")
    offers: list[CloudOffer] = []
    for raw in raw_offers:
        if not isinstance(raw, dict):
            continue
        price = raw.get("min_bid") if interruptible else raw.get("dph_total", raw.get("dph"))
        try:
            offers.append(
                CloudOffer(
                    provider="vast",
                    offer_id=str(raw["id"]),
                    gpu_model=str(raw["gpu_name"]),
                    gpu_count=int(raw["num_gpus"]),
                    hourly_usd=float(price),
                    reliability=float(raw["reliability"]),
                    verified=bool(raw.get("verified", False)),
                    interruptible=interruptible,
                    available=bool(raw.get("rentable", True)),
                    region=str(raw.get("geolocation", "")),
                    cuda_version=str(raw.get("cuda_vers", "")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("Vast returned a malformed offer") from exc
    return offers


def parse_runpod_offers(
    payload: dict[str, Any], *, cloud: str
) -> list[CloudOffer]:
    raw_gpus = payload.get("gpus")
    if not isinstance(raw_gpus, list):
        raise ProviderError("RunPod response is missing gpus")
    price_name = cloud.lower()
    offers: list[CloudOffer] = []
    for raw in raw_gpus:
        if not isinstance(raw, dict):
            continue
        price = raw.get("price", {}).get(price_name)
        if price is None:
            continue
        availability = str(raw.get("availability", "NONE")).upper()
        data_centers = raw.get("dataCenters") or []
        regions = ",".join(
            str(item.get("id"))
            for item in data_centers
            if isinstance(item, dict) and item.get("id")
        )
        cuda_versions = raw.get("cudaVersions") or []
        offers.append(
            CloudOffer(
                provider=f"runpod-{price_name}",
                offer_id=f"{raw['id']}:{cloud}",
                gpu_model=str(raw.get("name") or raw["id"]),
                gpu_count=1,
                hourly_usd=float(price),
                reliability=None,
                verified=cloud == "SECURE",
                interruptible=False,
                available=availability != "NONE",
                region=regions,
                cuda_version=",".join(str(value) for value in cuda_versions),
            )
        )
    return offers
