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




#UID 
curl -s "http://admin:admin@35.173.177.252:3000/api/datasources" | python3 -c "
import sys,json
for ds in json.load(sys.stdin):
    print(f'{ds[\"name\"]:20s}  uid={ds[\"uid\"]}  type={ds[\"type\"]}  url={ds.get(\"url\",\"\")}')"


source .env && sed "s/\${DS_PROMETHEUS}/${GRAFANA_PROMETHEUS_UID}/g" \
  grafana-dashboards/k8s-ec2-cluster-monitor.json | \
  curl -X POST "http://admin:admin@35.173.177.252:3000/api/dashboards/db" \
  -H "Content-Type: application/json" -d @-


python3 scripts/validate-dashboard.py




 ps -ef | grep -i grafana
ubuntu      8509       1  0 03:39 pts/1    00:00:02 /snap/kubectl/3816/kubectl port-forward --address 0.0.0.0 svc/grafana-service 3000:3000 -n mcp-test
472        27032   27008  9 04:56 ?        00:00:03 grafana server --homepath=/usr/share/grafana --config=/etc/grafana/grafana.ini --packaging=docker cfg:default.log.mode=console cfg:default.paths.data=/var/lib/grafana cfg:default.paths.logs=/var/log/grafana cfg:default.paths.plugins=/var/lib/grafana/plugins cfg:default.paths.provisioning=/etc/grafana/provisioning
ubuntu     27207   26698  0 04:57 pts/3    00:00:00 grep --color=auto -i grafana
ubuntu@ip-172-31-82-237:~/k8s-cost-copilot$ kill -9 8509
ubuntu@ip-172-31-82-237:~/k8s-cost-copilot$ sudo docker ps | grep grafana
ubuntu@ip-172-31-82-237:~/k8s-cost-copilot$ kubectl get pods -n mcp-test | grep -i grafana
grafana-deployment-867d4b5676-k4x8h      1/1     Running            3 (2m28s ago)   4d1h
ubuntu@ip-172-31-82-237:~/k8s-cost-copilot$ nohup kubectl port-forward --address 0.0.0.0 svc/grafana-service 3000:3000 -n mcp-test > grafana-pf.log 2>&1 &
[1] 27687



# Transfer updated files
scp -i C:\k8s_key.pem \
  "D:\Kubernetes & Cloud Cost Copilot\grafana-dashboards\k8s-ec2-cluster-monitor.json" \
  ubuntu@35.173.177.252:~/k8s-cost-copilot/grafana-dashboards/

# On EC2 — delete the old duplicate datasource
curl -X DELETE "http://admin:admin@35.173.177.252:3000/api/datasources/uid/bfqdd8vm09fr4a"

# Recreate ConfigMap + restart Grafana to pick up updated dashboard
kubectl create configmap grafana-dashboard-k8s-cluster \
  --from-file=k8s-ec2-cluster-monitor.json=grafana-dashboards/k8s-ec2-cluster-monitor.json \
  -n mcp-test --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout restart deployment/grafana-deployment -n mcp-test