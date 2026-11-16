"""Construye el flujo de recepción de producciones sin incorporar secretos."""

import json
import sys
import uuid

NAME = "Studio · recepción de producciones"
EDGE = "https://studio.e-scale.cl/api/internal/production/dispatch"
INBOX = "http://172.31.240.1:3160/v1/productions"


def workflow(*, node_ids, edge_credential, inbox_credential):
    if (
        len(node_ids) != 5
        or len(set(node_ids)) != 5
        or any(str(uuid.UUID(x)) != x for x in node_ids)
    ):
        raise ValueError("Identidades de nodos inválidas")
    if len(set((edge_credential, inbox_credential))) != 2:
        raise ValueError("Se requieren credenciales dedicadas")

    def node(name, kind, version, config, token=None):
        index = len(nodes)
        row = {
            "id": node_ids[index],
            "name": name,
            "type": "n8n-nodes-base." + kind,
            "typeVersion": version,
            "position": [index * 260, 0],
            "parameters": config,
        }
        if token:
            row["credentials"] = {"httpHeaderAuth": {"id": token}}
        nodes.append(row)

    def http(name, url, body, credential, timeout=10000):
        node(
            name,
            "httpRequest",
            4.2,
            {
                "method": "POST",
                "url": url,
                "authentication": "genericCredentialType",
                "genericAuthType": "httpHeaderAuth",
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": body,
                "options": {
                    "timeout": timeout,
                    "redirect": {"redirect": {"followRedirects": False}},
                },
            },
            credential,
        )

    nodes = []
    node(
        "Cada 15 segundos",
        "scheduleTrigger",
        1.2,
        {"rule": {"interval": [{"field": "seconds", "secondsInterval": 15}]}},
    )
    http("Reservar en Studio", EDGE, '{"action":"claim"}', edge_credential)
    node(
        "Hay una orden",
        "code",
        2,
        {"jsCode": "return $input.all().filter(item => item.json.job !== null);"},
    )
    http(
        "Guardar crudo verificado",
        INBOX,
        "={{ JSON.stringify($json.job) }}",
        inbox_credential,
        timeout=50000,
    )
    http(
        "Confirmar recepción",
        EDGE,
        "={{ JSON.stringify({action:'ack',order_id:$json.order_id,lease:$('Reservar en Studio').first().json.job.lease}) }}",
        edge_credential,
    )
    links = {
        left["name"]: {"main": [[{"node": right["name"], "type": "main", "index": 0}]]}
        for left, right in zip(nodes, nodes[1:], strict=False)
    }
    settings = {
        "executionOrder": "v1",
        "saveDataSuccessExecution": "none",
        "saveDataErrorExecution": "none",
        "saveManualExecutions": False,
        "saveExecutionProgress": False,
        "executionTimeout": 95,
    }
    return {"name": NAME, "nodes": nodes, "connections": links, "settings": settings}


if __name__ == "__main__":
    print(json.dumps(workflow(**json.load(sys.stdin)), ensure_ascii=False))
