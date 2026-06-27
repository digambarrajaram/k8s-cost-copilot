ubuntu@ip-172-31-82-237:~$ history
    1  sudo su
    2  minikube start --driver=docker
    3  sudo usermod -aG docker $USER && newgrp docker
    4  sudo usermod -aG docker $USER && newgrp docker
    5  pwd
    6  git clone https://github.com/digambarrajaram/k8s-cost-copilot.git
   12  kubectl apply -f mcp-deployment.yaml
   13  kubectl get all
   27  kubectl cluster-info
   28  kubectl get pods
   29  kubectl get nodes
   30  kubectl create namespace mcp
   31  kubectl get ns
   32  kubectl create serviceaccount mcp-viewer -n mcp
   33  kubectl get serviceaccounts
   34  kubectl get mcp-viewer
   35  kubectl create clusterrolebinding mcp-viewer-crb   --clusterrole=view   --serviceaccount=mcp:mcp-viewer
   42  minikube start
   43  kubectl get cluster
   70  # Download and execute the NodeSource installation script for Node v20
   71  curl -fsSL https://nodesource.com | sudo -E bash -
   72  # Install Node.js and npm
   73  sudo apt-get install -y nodejs
   74  node -v
   75  npm -v
   76  sudo apt install npm
   77  node -v
   78  npm -v
   79  npx @modelcontextprotocol/inspector npx -y kubernetes-mcp-server@latest
   84  npx -y kubernetes-mcp-server@latest --port 8080 --kubeconfig /home/ubuntu/.kube/config

    nohup npx -y kubernetes-mcp-server@latest --port 8080 --kubeconfig /home/ubuntu/.kube/config > mcp.log 2>&1 &


# Check the Prometheus URL
cat prometheus-tunnel.log

# Check the Grafana URL
cat grafana-tunnel.log


# 1. Force forward Prometheus to all interfaces
nohup kubectl port-forward --address 0.0.0.0 svc/prometheus-service 9090:9090 -n mcp-test > prom-pf.log 2>&1 &

# 2. Force forward Grafana to all interfaces
nohup kubectl port-forward --address 0.0.0.0 svc/grafana-service 3000:3000 -n mcp-test > grafana-pf.log 2>&1 &


minikube addons enable metrics-server







# ── 1. Start Minikube ──
minikube start

# ── 2. Wait for cluster to be ready ──
kubectl wait --for=condition=Ready node/minikube --timeout=120s

nohup npx -y kubernetes-mcp-server@latest --port 8080 --kubeconfig /home/ubuntu/.kube/config > mcp.log 2>&1 &


# ── 3. Start port-forwards (background) ──
pkill -f "port-forward" 2>/dev/null
sleep 2

nohup kubectl port-forward --address 0.0.0.0 svc/prometheus-service 9090:9090 -n mcp-test > ~/prom-pf.log 2>&1 &
nohup kubectl port-forward --address 0.0.0.0 svc/grafana-service 3000:3000 -n mcp-test > ~/grafana-pf.log 2>&1 &

# ── 4. Verify everything is up ──
sleep 5
kubectl get pods -n mcp-test
curl -s http://localhost:9090/-/healthy && echo "✅ Prometheus OK"
curl -s http://localhost:3000/api/health | python3 -c "import json,sys; d=json.load(sys.stdin); print('✅ Grafana OK' if d.get('database')=='ok' else '❌ Grafana issue')"




curl -X POST "http://admin:admin@52.70.236.20:3000/api/dashboards/db" \
  -H "Content-Type: application/json" \
  -d @grafana-dashboards/k8s-ec2-cluster-monitor.json




curl -s "http://admin:admin@35.173.177.252:3000/api/datasources" | python3 -c "
import sys,json
for ds in json.load(sys.stdin):
    print(f'{ds[\"name\"]:20s}  uid={ds[\"uid\"]}  type={ds[\"type\"]}  url={ds.get(\"url\",\"\")}')"



source .env && sed "s/\${DS_PROMETHEUS}/${GRAFANA_PROMETHEUS_UID}/g" \
  grafana-dashboards/k8s-ec2-cluster-monitor.json | \
  curl -X POST "http://admin:admin@52.70.236.20:3000/api/dashboards/db" \
  -H "Content-Type: application/json" -d @-

