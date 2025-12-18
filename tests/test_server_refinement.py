
import subprocess
import time
import requests
import pytest
from pathlib import Path
import json

SERVER_URL = "http://127.0.0.1:8100/api/v1"

@pytest.fixture(scope="module")
def server_process():
    """Start the server in a separate process."""
    proc = subprocess.Popen(
        ["uv", "run", "knowcode", "server", "--port", "8100", "--store", "."],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    
    # Wait for server to start
    for _ in range(20):
        try:
            requests.get(f"{SERVER_URL}/health")
            break
        except requests.ConnectionError:
            time.sleep(0.5)
    else:
        proc.terminate()
        raise RuntimeError("Server failed to start")
        
    yield proc
    
    proc.terminate()
    proc.wait()

def test_reload_endpoint(server_process):
    """Test that the /reload endpoint works."""
    response = requests.post(f"{SERVER_URL}/reload")
    assert response.status_code == 200
    assert response.json()["status"] == "reloaded"

def test_get_entity(server_process):
    """Test getting a specific entity."""
    # First search to get a valid ID
    search_resp = requests.get(f"{SERVER_URL}/search", params={"q": "GraphBuilder"})
    assert search_resp.status_code == 200
    results = search_resp.json()
    assert len(results) > 0
    
    entity_id = results[0]["id"]
    
    # Get details
    details_resp = requests.get(f"{SERVER_URL}/entities/{entity_id}")
    assert details_resp.status_code == 200
    details = details_resp.json()
    
    assert details["id"] == entity_id
    assert "source_code" in details
    assert "location" in details

def test_entity_not_found(server_process):
    """Test error handling for missing entity."""
    response = requests.get(f"{SERVER_URL}/entities/non_existent_id")
    assert response.status_code == 404
