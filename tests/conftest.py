import pytest
from fastapi.testclient import TestClient

from faktura.app.api import app, load_context
from faktura.app.auth import DevHeaderAuth
from faktura.app.db import Base, make_engine, make_sessionmaker
from faktura.evals.extraction import load_labels
from faktura.synth.cli import write_dataset
from faktura.synth.config import SynthConfig
from faktura.synth.generate import generate
from tests.helpers import STAFF


@pytest.fixture(scope="session")
def app_data_dir(tmp_path_factory):
    cfg = SynthConfig(seed=41, n_purchase_orders=40, anomaly_rate=0.4)
    out = tmp_path_factory.mktemp("app")
    write_dataset(generate(cfg), cfg, out, render=True)
    return out


@pytest.fixture()
def env(app_data_dir):
    """API client on a fresh in-memory DB, header auth; seed invoices with helpers.ingest."""
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    app.state.sessions = make_sessionmaker(engine)
    app.state.ctx = load_context(app_data_dir)
    app.state.auth = DevHeaderAuth()
    labels = load_labels(app_data_dir)
    with TestClient(app, headers=STAFF) as client:
        yield client, labels, app_data_dir
    engine.dispose()
