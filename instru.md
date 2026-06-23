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
