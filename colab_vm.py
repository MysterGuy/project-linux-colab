ACTION = 'start'
RESOLUTION = '1280x720'
BACKUP_MINUTES = 60
CLIENTS = {'Sao Paulo': '', 'Spain': ''}
INSTALL_APPS = True
RESTORE_BACKUP = True
import os, re, json, pwd, shutil, secrets, socket, subprocess, time
import threading, tempfile, tarfile, gzip, hashlib, ipaddress
from pathlib import Path
from datetime import datetime, timezone
BASE = Path('/tmp/colab-vnc-userspace')
VM_HOME = Path('/home/vncuser')
DRIVE = Path('/content/drive')
PERSIST = DRIVE / 'MyDrive/ColabVM'
SNAPSHOTS = PERSIST / 'snapshots'
EXCLUDED = {'.cache', '.config/colab-vnc', '.local/share/Trash'}

def run_command(args, timeout=1800, check=True, env=None):
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    log = BASE / 'command.log'
    with log.open('w') as out:
        p = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, env=env)
        try:
            start_time = time.monotonic()
            announced = False
            while p.poll() is None:
                try:
                    p.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    if not announced and time.monotonic() - start_time >= 60:
                        print(Path(args[0]).name, 'running', flush=True)
                        announced = True
                if time.monotonic() - start_time > timeout:
                    raise TimeoutError(f'{args[0]} exceeded {timeout}s')
        finally:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
    result = subprocess.CompletedProcess(args, p.returncode, log.read_text(errors='replace'))
    if check and p.returncode:
        raise RuntimeError(f'{args[0]} failed:\n{result.stdout[-5000:]}')
    return result

def write_file(path, message, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(message)
    path.chmod(mode)

def download(url, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.download')
    run_command(['curl', '-fL', '--retry', '4', '--connect-timeout', '30', '--max-time', '900', url, '-o', str(partial)], timeout=1000)
    os.replace(partial, target)

def check_drive():
    if not os.path.ismount(DRIVE) or not (DRIVE / 'MyDrive').is_dir():
        raise RuntimeError('Google Drive is not mounted.')

def backup_entries():
    for root, dirs, files in os.walk(VM_HOME, followlinks=False):
        folder = Path(root)
        dirs[:] = sorted((d for d in dirs if (folder / d).relative_to(VM_HOME).as_posix() not in EXCLUDED))
        for name in sorted(dirs + files):
            entry = folder / name
            rel = entry.relative_to(VM_HOME).as_posix()
            if rel in EXCLUDED:
                continue
            try:
                st = entry.lstat()
            except FileNotFoundError:
                continue
            yield (entry, rel, st)

def signature():
    h = hashlib.sha256()
    total_size = 0
    for entry, rel, st in backup_entries():
        h.update(f'{rel}\x00{st.st_mode}\x00{st.st_size}\x00{st.st_mtime_ns}'.encode())
        if entry.is_file() and (not entry.is_symlink()):
            total_size += st.st_size
    return (h.hexdigest(), total_size)

def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def backup(force=True):
    check_drive()
    lock = globals().setdefault('_BACKUP_LOCK', threading.Lock())
    if not lock.acquire(blocking=False):
        print('Backup already running.', flush=True)
        return False
    try:
        if not VM_HOME.is_dir():
            raise RuntimeError('vncuser does not exist.')
        current, total_size = signature()
        if not force and current == globals().get('_LAST_SIGNATURE'):
            return True
        if shutil.disk_usage('/tmp').free < total_size + 512 * 1024 ** 2:
            raise RuntimeError('Not enough local disk space for backup.')
        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        name = datetime.now(timezone.utc).strftime('home-%Y%m%dT%H%M%S-') + secrets.token_hex(3)
        print('Creating backup...', flush=True)
        with tempfile.TemporaryDirectory(prefix='colab-backup-') as temp:
            local = Path(temp) / (name + '.tar.gz')
            with local.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=1) as gz:
                with tarfile.open(fileobj=gz, mode='w|') as tar:
                    for entry, rel, st in backup_entries():
                        if entry.is_socket() or entry.is_fifo():
                            continue
                        try:
                            tar.add(entry, arcname=rel, recursive=False)
                        except FileNotFoundError:
                            raise RuntimeError('Files changed during backup. Close applications and retry.')
            with gzip.open(local, 'rb') as f:
                while f.read(4 * 1024 * 1024):
                    pass
            digest = sha256_file(local)
            partial = SNAPSHOTS / (name + '.tar.gz.partial')
            with local.open('rb') as src, partial.open('wb') as dst:
                shutil.copyfileobj(src, dst, length=4 * 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            if sha256_file(partial) != digest:
                raise RuntimeError('Backup verification failed.')
            final = SNAPSHOTS / (name + '.tar.gz')
            os.replace(partial, final)
            manifest = {'file': final.name, 'sha256': digest, 'date_utc': datetime.now(timezone.utc).isoformat(), 'size_bytes': final.stat().st_size}
            partial_meta = SNAPSHOTS / (name + '.json.partial')
            write_file(partial_meta, json.dumps(manifest, indent=2))
            os.replace(partial_meta, SNAPSHOTS / (name + '.json'))
        if signature()[0] == current:
            globals()['_LAST_SIGNATURE'] = current
        else:
            globals().pop('_LAST_SIGNATURE', None)
            print('Files changed during backup.')
        globals()['_LAST_BACKUP'] = manifest
        print('Backup complete:', final.name, flush=True)
        return True
    finally:
        lock.release()

def restore():
    marker = BASE / 'home-ready'
    if marker.exists() or not RESTORE_BACKUP:
        return
    manifests = sorted(SNAPSHOTS.glob('home-*.json'), reverse=True) if SNAPSHOTS.exists() else []
    if not manifests:
        old_path = PERSIST / 'vncuser'
        if old_path.exists():
            print('Legacy backup found.')
            raise RuntimeError('Legacy backup requires migration.')
        print('No backup found.')
        marker.touch()
        return
    if any(((VM_HOME / d).exists() for d in ('Games', '.wine', '.mozilla'))):
        raise RuntimeError('Local data exists; restore stopped to avoid overwriting it.')
    metadata = json.loads(manifests[0].read_text())
    name = metadata.get('file', metadata.get('arquivo'))
    if not re.fullmatch('home-\\d{8}T\\d{6}-[0-9a-f]{6}\\.tar\\.gz', name):
        raise RuntimeError('Invalid snapshot name.')
    source_path = SNAPSHOTS / name
    print('Restoring:', name, flush=True)
    with tempfile.TemporaryDirectory(prefix='colab-restore-') as temp:
        local = Path(temp) / name
        shutil.copyfile(source_path, local)
        if sha256_file(local) != metadata['sha256']:
            raise RuntimeError('Snapshot checksum mismatch.')
        if not hasattr(tarfile, 'data_filter'):
            raise RuntimeError('Safe tar extraction is unavailable.')
        skipped = []

        def safe_filter(member, destination):
            try:
                return tarfile.data_filter(member, destination)
            except tarfile.FilterError:
                skipped.append(member.name)
                return None
        with tarfile.open(local, 'r:gz') as tar:
            tar.extractall(VM_HOME, filter=safe_filter)
        if skipped:
            print('Skipped files:', skipped[:10])
    for dos in VM_HOME.rglob('dosdevices'):
        if dos.is_dir() and (not dos.is_symlink()):
            for link_name, target in (('c:', '../drive_c'), ('z:', '/')):
                link = dos / link_name
                if not os.path.lexists(link):
                    link.symlink_to(target)
    account = pwd.getpwnam('vncuser')
    run_command(['chown', '-hR', f'{account.pw_uid}:{account.pw_gid}', str(VM_HOME)])
    marker.touch()
    print('Restore complete.')

def start_process(args, name):
    log = BASE / (name + '.log')
    with log.open('w') as f:
        p = subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
    globals().setdefault('VNC_PROCESSES', []).append(p)
    return p

def install_apps():
    if not INSTALL_APPS:
        return
    if not Path('/opt/firefox/firefox').exists():
        download('https://download.mozilla.org/?product=firefox-latest-ssl&os=linux64&lang=pt-BR', BASE / 'firefox.tar.xz')
        run_command(['tar', '-xf', str(BASE / 'firefox.tar.xz'), '-C', '/opt'])
    run_command(['ln', '-sfn', '/opt/firefox/firefox', '/usr/local/bin/firefox'])
    if not Path('/opt/heroic/squashfs-root/AppRun').exists():
        download('https://api.github.com/repos/Heroic-Games-Launcher/HeroicGamesLauncher/releases/latest', BASE / 'heroic-release.json')
        release = json.loads((BASE / 'heroic-release.json').read_text())
        assets = [a for a in release['assets'] if a['name'].endswith('.AppImage') and (not re.search('arm|aarch', a['name'], re.I))]
        if len(assets) != 1:
            raise RuntimeError('Could not select an amd64 Heroic AppImage.')
        download(assets[0]['browser_download_url'], '/opt/heroic/Heroic.AppImage')
        Path('/opt/heroic/Heroic.AppImage').chmod(0o755)
        run_command(['sh', '-c', 'cd /opt/heroic && ./Heroic.AppImage --appimage-extract'])
        sandbox = Path('/opt/heroic/squashfs-root/chrome-sandbox')
        if sandbox.exists():
            os.chown(sandbox, 0, 0)
            sandbox.chmod(0o4755)
    write_file('/usr/local/bin/heroic', '#!/bin/sh\n\nexport XDG_CONFIG_HOME="$HOME/.config"\n\nexport XDG_CACHE_HOME="$HOME/.cache"\n\nexport XDG_DATA_HOME="$HOME/.local/share"\n\nexec /opt/heroic/squashfs-root/AppRun "$@"\n\n', 0o755)
    for name, cmd in (('Firefox', 'firefox'), ('Heroic', 'heroic'), ('Steam', 'steam')):
        message = f'[Desktop Entry]\nName={name}\nExec={cmd}\nType=Application\nTerminal=false\nIcon=applications-games\nCategories=Game;\n'
        write_file(f'/usr/local/share/applications/{cmd}.desktop', message)
        write_file(VM_HOME / f'Desktop/{name}.desktop', message, 0o755)

def status_vm():
    print('Display :21:', Path('/tmp/.X11-unix/X21').exists())
    print('Logs:', BASE)
    print('Last backup:', globals().get('_LAST_BACKUP', 'none'))
    info = globals().get('_VNC_INFO')
    if info:
        print('VNC password:', info['password'])
    if globals().get('VNC_CLI'):
        print(run_command(VNC_CLI + ['status'], timeout=15, check=False).stdout)
    if shutil.which('nvidia-smi'):
        print(run_command(['nvidia-smi', '--query-gpu=name,driver_version', '--format=csv,noheader'], timeout=15, check=False).stdout)

def main():
    if ACTION not in {'start', 'backup', 'status'}:
        raise ValueError('ACTION must be start, backup, or status.')
    if os.geteuid() != 0 or not Path('/etc/os-release').exists():
        raise RuntimeError('Run as root in a Colab Linux runtime.')
    if not re.search('^ID=ubuntu$', Path('/etc/os-release').read_text(), re.M):
        raise RuntimeError('Ubuntu is required.')
    if not re.fullmatch('\\d{3,4}x\\d{3,4}', RESOLUTION):
        raise ValueError('Invalid resolution.')
    if BACKUP_MINUTES < 5:
        raise ValueError('BACKUP_MINUTES must be at least 5.')
    from google.colab import drive
    if not os.path.ismount(DRIVE):
        drive.mount(str(DRIVE))
    check_drive()
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    BASE.chmod(0o700)
    PERSIST.mkdir(parents=True, exist_ok=True)
    try:
        ipy = get_ipython()
        source_code = ipy.history_manager.input_hist_raw[-1]
        if 'def main():' in source_code and 'RESTORE_BACKUP' in source_code:
            write_file(PERSIST / 'colab_vm.py', source_code)
    except (NameError, AttributeError, IndexError):
        pass
    if ACTION == 'backup':
        backup()
        return
    if ACTION == 'status':
        status_vm()
        return
    if Path('/tmp/.X11-unix/X21').exists():
        print('Display already running.')
        status_vm()
        return
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 5921))
    for name, ip in CLIENTS.items():
        if ip:
            ipaddress.ip_address(ip)
    env = dict(os.environ, DEBIAN_FRONTEND='noninteractive')
    run_command(['dpkg', '--add-architecture', 'i386'], env=env)
    run_command(['apt-get', 'update'], env=env)
    run_command(['apt-get', 'install', '-y', 'software-properties-common', 'curl', 'ca-certificates'], env=env)
    for repo in ('universe', 'multiverse'):
        run_command(['add-apt-repository', '-y', repo], env=env)
    run_command(['apt-get', 'update'], env=env)
    packages = ['tigervnc-standalone-server', 'tigervnc-tools', 'xfce4-session', 'xfce4-panel', 'xfce4-settings', 'xfwm4', 'xfdesktop4', 'thunar', 'xfce4-terminal', 'dbus', 'dbus-x11', 'xauth', 'x11-utils', 'x11-xserver-utils', 'sudo', 'mesa-utils']
    if INSTALL_APPS:
        packages += ['wine', 'wine64', 'wine32:i386', 'winetricks', 'steam-installer', 'mangohud', 'gamemode', 'vulkan-tools', 'libvulkan1', 'libvulkan1:i386', 'mesa-vulkan-drivers', 'mesa-vulkan-drivers:i386', 'xz-utils', 'unzip', 'libnss3', 'libxss1', 'libgbm1']
        for new_package, old_path in (('libfuse2t64', 'libfuse2'), ('libgtk-3-0t64', 'libgtk-3-0'), ('libasound2t64', 'libasound2')):
            policy = run_command(['apt-cache', 'policy', new_package], timeout=30).stdout
            packages.append(new_package if re.search(r'Candidate:\s+(?!\(none\))\S+', policy) else old_path)
    print('Installing packages...', flush=True)
    run_command(['apt-get', 'install', '-y', *packages], env=env)
    try:
        account = pwd.getpwnam('vncuser')
    except KeyError:
        run_command(['useradd', '-m', '-s', '/bin/bash', 'vncuser'])
        account = pwd.getpwnam('vncuser')
    restore()
    for folder in ('Desktop', 'Downloads', 'Games', '.config', '.cache', '.local/share'):
        path = VM_HOME / folder
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, account.pw_uid, account.pw_gid)
    install_apps()
    run_command(['chown', '-hR', 'vncuser:vncuser', str(VM_HOME / 'Desktop')])
    run_command(['dbus-uuidgen', '--ensure=/etc/machine-id'])
    Path('/run/dbus').mkdir(parents=True, exist_ok=True)
    if not Path('/run/dbus/system_bus_socket').exists():
        Path('/run/dbus/pid').unlink(missing_ok=True)
        run_command(['dbus-daemon', '--system', '--fork'])
    if not shutil.which('tailscaled') or not shutil.which('tailscale'):
        download('https://tailscale.com/install.sh', BASE / 'install-tailscale.sh')
        run_command(['sh', str(BASE / 'install-tailscale.sh')], env=env)
    work = VM_HOME / '.config/colab-vnc'
    work.mkdir(parents=True, exist_ok=True)
    work.chmod(0o700)
    password_text = ''.join((secrets.choice('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8)))
    passwd_bin = shutil.which('tigervncpasswd') or shutil.which('vncpasswd')
    if not passwd_bin:
        raise RuntimeError('VNC password tool not found.')
    encoded = subprocess.run([passwd_bin, '-f'], input=(password_text + '\n').encode(), capture_output=True, check=True, timeout=10).stdout
    globals()['_VNC_INFO'] = {'password': password_text}
    password = work / 'passwd'
    password.write_bytes(encoded)
    password.chmod(0o600)
    auth = work / 'Xauthority'
    auth.touch(mode=0o600, exist_ok=True)
    run_command(['xauth', '-f', str(auth), 'add', ':21', '.', secrets.token_hex(16)])
    runtime = Path('/tmp/runtime-vncuser')
    runtime.mkdir(exist_ok=True)
    runtime.chmod(0o700)
    os.chown(runtime, account.pw_uid, account.pw_gid)
    run_command(['chown', '-hR', 'vncuser:vncuser', str(work)])
    user_environment = ['HOME=/home/vncuser', 'USER=vncuser', 'LOGNAME=vncuser', 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games', 'XDG_RUNTIME_DIR=/tmp/runtime-vncuser', 'XDG_CONFIG_HOME=/home/vncuser/.config', 'XDG_CONFIG_DIRS=/etc/xdg/xdg-xubuntu:/etc/xdg', 'XDG_DATA_DIRS=/usr/local/share:/usr/share', 'DISPLAY=:21', f'XAUTHORITY={auth}', 'XDG_SESSION_TYPE=x11', 'XDG_CURRENT_DESKTOP=XFCE', 'XDG_SESSION_DESKTOP=xfce', 'DESKTOP_SESSION=xfce']
    prefix = ['sudo', '-u', 'vncuser', 'env', '-i', *user_environment]
    xvnc = shutil.which('Xtigervnc') or shutil.which('Xvnc')
    if not xvnc:
        raise RuntimeError('VNC server not found.')
    server = start_process(prefix + [xvnc, ':21', '-geometry', RESOLUTION, '-depth', '24', '-rfbport', '5921', '-localhost', 'yes', '-SecurityTypes', 'VncAuth', '-rfbauth', str(password), '-auth', str(auth), '-nolisten', 'tcp', '-AlwaysShared'], 'vnc')
    for _ in range(30):
        if server.poll() is not None:
            raise RuntimeError((BASE / 'vnc.log').read_text(errors='replace')[-4000:])
        if run_command(prefix + ['xdpyinfo'], timeout=5, check=False).returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('VNC display did not start.')
    desktop_script = "xsetroot -solid '#263238'\n\nxset s off\n\nxset -dpms 2>/dev/null || true\n\nxfsettingsd --replace &\n\nsleep 2\n\nxfconf-query -c xfwm4 -p /general/use_compositing -n -t bool -s false || true\n\nxfwm4 --replace &\n\nsleep 2\n\nxfce4-panel &\n\nxfdesktop &\n\nThunar --daemon &\n\nsleep 2\n\nxfce4-terminal --disable-server --title='Ubuntu Colab' &\n\nwait\n\n"
    desktop = start_process(prefix + ['dbus-run-session', '--', 'sh', '-c', desktop_script], 'desktop')
    time.sleep(8)
    for program in ('xfwm4', 'xfce4-panel', 'xfdesktop'):
        if run_command(['pgrep', '-u', 'vncuser', '-x', program], check=False).returncode:
            raise RuntimeError(f'{program} did not start. See {BASE}/desktop.log')
    try:
        backup()
    except Exception as exc:
        print('Backup failed:', exc, flush=True)
    stop = globals().get('_BACKUP_STOP')
    if stop:
        stop.set()
    stop = threading.Event()
    globals()['_BACKUP_STOP'] = stop

    def loop_backup():
        while not stop.wait(BACKUP_MINUTES * 60):
            try:
                backup(force=False)
            except Exception as e:
                print('Backup failed:', str(e), flush=True)
    threading.Thread(target=loop_backup, daemon=True, name='colab-backup').start()
    sock = str(BASE / 'tailscaled.sock')
    if not Path(sock).exists():
        ts = start_process(['tailscaled', '--tun=userspace-networking', '--socket=' + sock, '--state=' + str(BASE / 'tailscale.state')], 'tailscaled')
        for _ in range(30):
            if ts.poll() is not None:
                raise RuntimeError((BASE / 'tailscaled.log').read_text(errors='replace')[-4000:])
            if Path(sock).exists():
                break
            time.sleep(1)
        else:
            raise RuntimeError('Tailscale did not start.')
    global VNC_CLI
    VNC_CLI = ['tailscale', '--socket=' + sock]
    login = start_process(VNC_CLI + ['up', '--accept-dns=false', '--accept-routes=false', '--hostname=colab-vnc', '--timeout=5m'], 'login')
    seen = set()
    for _ in range(155):
        message = (BASE / 'login.log').read_text(errors='replace')
        for url in re.findall('https://login\\.tailscale\\.com/\\S+', message):
            if url not in seen:
                print('Tailscale login:', url, flush=True)
                seen.add(url)
        st = run_command(VNC_CLI + ['status', '--json'], timeout=10, check=False)
        try:
            ready = json.loads(st.stdout).get('BackendState') == 'Running'
        except ValueError:
            ready = False
        if ready:
            break
        if login.poll() is not None and login.returncode:
            raise RuntimeError(message[-3000:])
        time.sleep(2)
    else:
        raise RuntimeError('Tailscale login timed out. See login.log.')
    run_command(VNC_CLI + ['serve', '--bg', '--tcp=5901', 'tcp://127.0.0.1:5921'], timeout=45)
    ip = run_command(VNC_CLI + ['ip', '-4'], timeout=15).stdout.strip().splitlines()[0]
    if desktop.poll() is not None:
        raise RuntimeError('Desktop stopped. See desktop.log.')
    print('\nDesktop ready')
    print('TigerVNC Viewer:', ip + '::5901', '| Password:', password_text)
    print('Games:', VM_HOME / 'Games')
    for name, client in CLIENTS.items():
        if client:
            print(name, run_command(VNC_CLI + ['ping', '--c=5', '--until-direct=false', '--timeout=3s', client], timeout=25, check=False).stdout)
main()
