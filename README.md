

_KPSML-X is designed to make file management seamless, fast, and flexible._

- **🌐 Universal Downloader** - Supports torrents, Mega, Google Drive, direct links, and all `yt-dlp` sites.  

- **☁️ Cloud Uploader** - Upload files to Google Drive, Telegram Cloud, Rclone, or DDL servers with ease.  

- **📦 Smart File Handling** - Automatic renaming, metadata tagging, and organization.  

- **🧠 Intelligent Automation** - Auto-resume, retry, and cleanup for 24×7 reliability.  

- **⚙️ Advanced Controls** - Manage downloads, uploads, and settings directly from Telegram (`/bs`, `/mirror`, `/leech`).  

- **🎯 Multi-Deployment Ready** - Deploy on Heroku, Docker, VPS, or Google Colab.  

- **🔐 Secure & Private** - Owner-only commands, user whitelisting, and access control.  

- **💨 Lightweight Performance** - Optimized Python & Pyrogram async engine for speed.  

</details>

---

## 🚀 Deployment Guide (VPS)


#Installing Requirements

Clone this repository:

 Build and Run the Docker Image

*Make sure you mount the app folder and install Docker following the official documentation.*

There are two methods to build and run the Docker image:

### 3.1 Using Official Docker Commands

- **Start Docker daemon** (skip if already running):

  ```bash
  sudo dockerd
  ```

- **Build the Docker image:**

  ```bash
  sudo docker build . -t kpsmlx
  ```

- **Run the image:**

  ```bash
  sudo docker run -p 80:80 -p 8080:8080 kpsmlx
  ```

- **To stop the running image:**

  First, list running containers:

  ```bash
  sudo docker ps
  ```

  Then, stop the container using its ID:

  ```bash
  sudo docker stop <container_id>
  ```

---

### 3.2 Using docker-compose (Recommended)

**Note:** If you want to use ports other than 80 and 8080 for torrent file selection and rclone serve respectively, update them in [docker-compose.yml](https://github.com/Tamilupdates/KPSML-X/blob/main/docker-compose.yml).

- **Install docker-compose:**

  ```bash
  sudo apt install docker-compose
  ```

- **Build and run the Docker image (or view the current running image):**

  ```bash
  sudo docker-compose up
  ```

- **After editing files (e.g., using nano to edit start.sh), rebuild:**

  ```bash
  sudo docker-compose up --build
  ```

- **To stop the running image:**

  ```bash
  sudo docker-compose stop
  ```

- **To restart the image:**

  ```bash
  sudo docker-compose start
  ```

- **To view the latest logs from the running container (after mounting the folder):**

  ```bash
  sudo docker-compose up
  ```

- **Tutorial Video for docker-compose and checking ports:**

  [![See Video](https://img.shields.io/badge/See%20Video-black?style=for-the-badge&logo=YouTube)](https://youtu.be/c8_TU1sPK08)


------

#### Docker Notes

**IMPORTANT NOTES**:

1. Set `BASE_URL_PORT` and `RCLONE_SERVE_PORT` variables to any port you want to use. Default is `80` and `8080` respectively.
2. You should stop the running image before deleting the container and you should delete the container before the image.
3. To delete the container (this will not affect on the image):

```
sudo docker container prune
```

4. To delete te images:

```
sudo docker image prune -a
```

5. Check the number of processing units of your machine with `nproc` cmd and times it by 4, then edit `AsyncIOThreadsCount` in qBittorrent.conf.
    
  </li></ol>
</details>

---


## 🛠️ Variables Descriptions

<details>
  <summary><b>View All Variables  <kbd>Click Here</kbd></b></summary>

- `BOT_TOKEN`: Telegram Bot Token that you got from [BotFather](https://t.me/BotFather). `Str`

- `OWNER_ID`: Telegram User ID (not username) of the Owner of the bot. `Int`

- `TELEGRAM_API`: This is to authenticate your Telegram account for downloading Telegram files. You can get this from <https://my.telegram.org>. `Int`

- `TELEGRAM_HASH`: This is to authenticate your Telegram account for downloading Telegram files. You can get this from <https://my.telegram.org>. `Str`

- `BASE_URL`: Valid BASE URL where the bot is deployed to use torrent web files selection.
  - ***Heroku Deployment***: Format of URL should be `https://app-name-random_code.herokuapp.com/`, where `app-name` is the name of your heroku app Paste the URL got when the App was Made. `Str`

  - ***VPS Deployment***: Format of URL should be `http://myip`, where `myip` is the IP/Domain(public) of your bot or if you have chosen port other than `80` so write it in this format `http://myip:port` (`http` and not `https`). `Str`

- `DATABASE_URL`: Database URL of MongoDb to store all your files and Vars. Adding this will be Helpful. `Str`

- `UPSTREAM_REPO`: GitLab repository URL, if your repo is private add `https://username:{githubtoken}@github.com/{username}/{reponame}` format. `Str`.
    - **NOTE**:
        - Any change in docker you need to deploy/build again with updated repo to take effect. 
        - **No Need to delete .gitignore file or any File**

- `UPSTREAM_BRANCH`: Upstream branch for update. Default is `kpsmlx`. `Str`

</details>

