"""
升学e网通(ewt360.com) 自动化学习工具
作者: teea418 (teea418@qq.com)
仓库: https://github.com/teea418/EazyPassEWT

通过 Playwright 模拟浏览器操作，自动登录平台、进入学习计划、
逐个播放未完成的视频任务，并支持在播放期间自动处理弹窗。

依赖: pip install -r requirements.txt && playwright install chromium
配置: 复制 .env.example 为 .env，填入账号密码后运行
"""

import sys, re, time, os, logging, random
from datetime import datetime
from typing import List, Dict, Optional, Callable, Any, NoReturn
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeoutError, Error as PwError, Page

# 优先从 .env 加载配置（避免硬编码凭据）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # 无 python-dotenv 时回退到环境变量或 .env 手动解析


# ── 日志系统 ──────────────────────────────────────────────

def setup_logger() -> logging.Logger:
    """
    配置双输出日志：控制台(INFO及以上) + 文件(全部级别)
    日志文件按运行时间命名，存放在 logs/ 目录下，方便回溯每次执行过程
    """
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"EazyPassEWT_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log")

    _logger = logging.getLogger("EazyPassEWT")
    _logger.setLevel(logging.DEBUG)

    # 防止模块热重载时重复添加 handler
    if not _logger.handlers:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        ))
        _logger.addHandler(fh)

        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter(
            "[%(asctime)s]：%(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        ))
        _logger.addHandler(ch)

    return _logger


logger = setup_logger()

# 模块级浏览器引用，由入口赋值，quit_ewt 中安全关闭
browser = None


# ── 通用工具 ──────────────────────────────────────────────

def retry_call(
    func: Callable,
    args: tuple = (),
    kwargs: Optional[dict] = None,
    max_attempts: int = 3,
    delay: float = 2.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,),
    on_retry: Optional[Callable] = None,
) -> Any:
    """
    指数退避重试工具

    网络请求、页面跳转等不稳定操作通过此函数包裹，
    失败后等待 delay * backoff^attempt 秒再试，避免因瞬时异常直接退出。

    最后一次重试仍失败则向上抛出异常，由调用方决定如何处理。
    """
    if kwargs is None:
        kwargs = {}
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except exceptions as e:
            last_exc = e
            if attempt < max_attempts:
                sleep_time = delay * (backoff ** (attempt - 1))
                logger.warning("%s 失败(第%d/%d次)，%.1fs后重试: %s",
                               func.__name__, attempt, max_attempts, sleep_time, e)
                if on_retry:
                    on_retry()
                time.sleep(sleep_time)
            else:
                logger.error("%s 已重试%d次，全部失败: %s",
                             func.__name__, max_attempts, e)
    raise last_exc  # type: ignore[misc]


def quit_ewt(code: int = 0) -> NoReturn:
    """
    安全退出：关闭浏览器后结束进程
    code=1 表示异常退出，暂停等待用户确认后再关闭
    """
    if code == 1:
        logger.error("EazyPassEWT 出现异常")
        input("按任意键退出EazyPassEWT...")
        code = 0
    if browser is not None:
        browser.close()
        logger.info("已关闭浏览器")
    logger.info("EazyPassEWT 将退出")
    sys.exit(code)


def element_exists(page: Page, selector: str, visible: bool = False, timeout: float = 1000) -> bool:
    """
    判断页面中是否存在匹配 selector 的元素
    visible=True 时要求元素在视口中可见（非隐藏、宽高>0）
    timeout 控制等待时长，超时视为不存在，不抛异常
    """
    try:
        if visible:
            page.locator(selector).first.wait_for(state="visible", timeout=timeout)
        else:
            page.locator(selector).first.wait_for(state="attached", timeout=timeout)
        return True
    except PwTimeoutError:
        return False


# ── 核心业务逻辑 ──────────────────────────────────────────

def login(page: Page, username: str, password: str, url: str):
    """
    登录 ewt360 平台

    已登录（检测"退出登录"按钮）则直接跳过；
    未登录则依次填充账号、密码，勾选隐私协议后提交。
    超时异常通过 raise 抛出，由外层 retry_call 处理。
    """
    try:
        page.goto(url, timeout=10000)
        page.wait_for_load_state("domcontentloaded")
        logger.info(f"打开网页：{page.title()}({page.url})")

        # 检测是否已登录（页面左上角有退出按钮说明已登录）
        if element_exists(page=page,
                          selector='button[type="button"]:has-text("退出登录")',
                          visible=True):
            masked = f"{username[:2]}****{username[-2:]}" if len(username) > 4 else "****"
            logger.info(f"账号：{masked} 已登录")
            return

        # ── 以下为首次登录流程 ──
        user_input = page.locator("#login__password_userName")
        user_input.wait_for(state="visible", timeout=5000)
        user_input.fill(username)
        # 日志脱敏：仅保留首尾各 2 位，中间用 **** 替代，避免完整用户名泄露到日志文件
        masked = f"{username[:2]}****{username[-2:]}" if len(username) > 4 else "****"
        logger.info(f"输入账号：{masked}")

        pwd_input = page.locator("#login__password_password")
        pwd_input.wait_for(state="visible", timeout=5000)
        pwd_input.fill(password)
        # 注意：生产环境日志应脱敏，此处密码仅用于调试阶段临时观察
        logger.info("输入密码：******")  # 日志中不输出明文密码

        # 勾选用户协议
        check = page.locator('div.privacy__agreement input[type="checkbox"]')
        check.wait_for(state="visible", timeout=5000)
        check.check()
        logger.info("勾选同意协议")

        login_btn = page.locator('button[type="submit"]:has-text("登 录")')
        login_btn.wait_for(state="visible", timeout=5000)
        with page.expect_navigation():
            login_btn.click()
        logger.info("点击登录按钮")

        # 登录后二次确认
        masked = f"{username[:2]}****{username[-2:]}" if len(username) > 4 else "****"
        if element_exists(page=page,
                          selector='button[type="button"]:has-text("退出登录")',
                          visible=True):
            logger.info(f"{masked} 已登录")
            return
        else:
            logger.error(f"{masked} 登录失败")
            quit_ewt(1)
    except PwTimeoutError:
        # 超时异常由外层 retry_call 统一处理，此处仅透传
        raise


def turn_to_homework(page: Page, url: str) -> Page:
    """
    从作业列表页找到第一个学习计划卡片，点击"开始学习"

    平台使用 SPA 架构，点击后会在新标签页打开详情页。
    函数返回新标签页的 Page 对象用于后续操作。
    """
    try:
        page.goto(url, timeout=10000)
        page.wait_for_load_state("domcontentloaded")
        logger.info(f"打开网页：{page.title()}({page.url})")

        # 等待卡片列表加载完毕
        page.wait_for_selector('li:has(button:has-text("开始学习"))', timeout=10000)
        cards = page.locator('li:has(button:has-text("开始学习"))').all()
        logger.info(f"找到 {len(cards)} 个卡片")

        # 遍历打印卡片标题，便于调试
        titles = []
        for idx, card in enumerate(cards, start=1):
            title_div = card.locator('div[class*="title-"]')
            try:
                title_text = title_div.first.inner_text(timeout=1000)
            except (PwTimeoutError, AttributeError):
                title_text = "未命名"
            titles.append(title_text)
            logger.info(f"{idx}. {title_text}")

        # 让用户选择卡片，仅有一个时默认选中
        if len(cards) == 1:
            choice = 1
            logger.info(f"仅有一个学习计划，默认选择：{titles[0]}")
        else:
            prompt = "\n".join(f"  {i}. {t}" for i, t in enumerate(titles, start=1))
            raw = input(f"发现 {len(cards)} 个学习计划，请选择（1-{len(cards)}，回车默认选1）：\n{prompt}\n> ")
            try:
                choice = int(raw.strip())
                if choice < 1 or choice > len(cards):
                    raise ValueError
            except (ValueError, AttributeError):
                choice = 1
                logger.info("输入无效，默认选择第一个")
        selected_card = cards[choice - 1]
        study_btn = selected_card.locator('button:has-text("开始学习")')
        study_btn.wait_for(state="visible", timeout=5000)

        # 点击后等待新标签页弹出
        with page.context.expect_page() as study_page_info:
            study_btn.click()
            logger.info(f"已点击计划：{titles[choice - 1]}")

        study_page = study_page_info.value
        if study_page is None:
            raise RuntimeError("打开学习页面失败：expect_page 返回 None")
        study_page.wait_for_load_state()
        logger.info(f"打开网页：{study_page.title()}({study_page.url})")
        return study_page
    except PwTimeoutError:
        raise


def get_schedule_list(page: Page) -> List[Dict]:
    """
    解析学习详情页中的每日打卡列表

    页面中 li[data-active] 是每日任务条目，data-active="text" 的是周标题需要过滤。
    每条记录包含日期和完成进度（如"完成3/5"），用于后续判断哪些任务需要执行。
    """
    page.wait_for_selector('li[data-active]', state="attached", timeout=10000)
    li_list = page.locator('li[data-active]:not([data-active="text"])').all()
    logger.info(f"找到 {len(li_list)} 个任务条目")

    res = []
    for li in li_list:
        # 每个任务 li 下有两个 span：第一个是日期，第二个是完成进度
        span_nodes = li.locator("span").all()
        if len(span_nodes) < 2:
            continue

        raw_date = span_nodes[0].text_content()
        clean_date = (raw_date or "").replace("今天", "").strip()
        complete_text = span_nodes[1].text_content() or ""

        item_dict = {
            "date": clean_date,
            "completion": complete_text,
            "locator": li,
        }
        res.append(item_dict)

    logger.info(f"提取到 {len(res)} 个有效条目")
    return res


def auto_choice(page: Page, schedule: list):
    """
    遍历每日打卡任务，自动筛选出当天未完成的视频并逐个播放

    策略：
    1. 优先淦今天的任务
    2. 今天的干完了，再回头补之前的（从大日期到小日期倒着补）
    3. 每个任务可能有多个视频，逐个在新标签页打开
    4. 视频播放带重试保护
    """
    logger.info("开始扫荡任务...")

    now = datetime.now()
    now_date_month = now.month
    now_date_day = now.day

    # 把所有日期解析出来分个类
    today_item = None
    past_items = []  # (month, day, item)

    for item in schedule:
        match = re.search(r'完成(\d+)/(\d+)', item["completion"])
        if not match:
            logger.error(f'{item["date"]} 任务：看不懂完成状态')
            quit_ewt(1)

        done = int(match.group(1))
        total = int(match.group(2))
        logger.info(f'{item["date"]} 任务：干了 {done}/{total}')

        # 全部完成直接跳过
        if done >= total:
            continue

        # 解析日期
        date_match = re.search(r'(\d{1,2})月(\d{1,2})日', item["date"])
        if not date_match:
            continue

        month = int(date_match.group(1))
        day = int(date_match.group(2))

        # 未来的任务不管
        if month > now_date_month or (month == now_date_month and day > now_date_day):
            logger.info(f'{item["date"]} 任务：还没到日子呢，跳过')
            continue

        if month == now_date_month and day == now_date_day:
            today_item = item
        else:
            past_items.append((month, day, item))

    # === 第一阶段：淦今天的 ===
    if today_item:
        logger.info(f'🔥 先淦今天的：{today_item["date"]}')
        _process_date(page, today_item)
    else:
        logger.info('👀 今天的任务已经全部干完了')

    # === 第二阶段：回头补之前的（从近到远倒着补）===
    if past_items:
        past_items.sort(key=lambda x: (x[0], x[1]), reverse=True)
        logger.info(f'📦 今天的搞完了，回头补之前的，共 {len(past_items)} 天有待办')
        for month, day, item in past_items:
            logger.info(f'⏪ 补 {item["date"]} 的任务')
            _process_date(page, item)

    logger.info('🎉 所有任务已扫荡完毕')


def _process_date(page: Page, item: dict):
    """处理某一天的任务：点进去、播视频"""
    try:
        item["locator"].click()
    except Exception:
        logger.warning(f'{item["date"]} 任务：点不进去，跳过')
        return
    page.wait_for_timeout(1000)

    buttons = page.locator('div[data-type="2"]')
    count = buttons.count() - 1
    logger.info(f'{item["date"]} 任务：翻到 {count} 个视频')

    for i in range(count):
        btn = buttons.nth(i)
        try:
            btn.wait_for(state="visible", timeout=5000)
        except Exception:
            continue

        # 检查是不是已经学完了
        if btn.get_attribute("data-finish") == "true":
            logger.info(f'{item["date"]} 任务：第 {i + 1} 个视频早就看完了，跳过')
            continue

        # 点开视频
        try:
            with page.context.expect_page(timeout=15000) as video_page_info:
                btn.click()
                logger.info(f'{item["date"]} 任务：冲第 {i + 1} 个视频')
            video_page = video_page_info.value
        except (PwTimeoutError, Exception):
            logger.warning(f'{item["date"]} 任务：第 {i + 1} 个视频没打开，算了')
            continue
        if video_page is None:
            continue

        video_page.wait_for_load_state()
        logger.info(f"打开视频：{video_page.title()}({video_page.url[:60]}...)")
        video_page.wait_for_load_state("networkidle")

        # 开淦，不行就重试3次
        retry_call(
            video_pass,
            args=(video_page,),
            kwargs={"monitor_selector": 'span[data-ac="check-pass"]'},
            max_attempts=3, delay=2.0,
            exceptions=(PwTimeoutError, Exception),
        )

        # 关掉视频标签页
        try:
            video_page.close()
            logger.info("视频看完了，关掉")
        except (PwError, Exception):
            pass

        # 歇口气再搞下一个
        interval = round(random.uniform(2.0, 8.0), 1)
        logger.info(f"歇 {interval}s 再战")
        time.sleep(interval)


# ── 视频播放相关 ──────────────────────────────────────────

def set_video_speed(page: Page, speed: float) -> bool:
    """
    通过 JavaScript 直接修改 video 标签的 playbackRate 属性
    比点击播放器倍速按钮更稳定可靠，失败时返回 False 不影响主流程
    """
    try:
        page.wait_for_selector('video.vjs-tech', timeout=5000)
        page.evaluate(f'''
            () => {{
                const video = document.querySelector('video.vjs-tech');
                if (!video) return false;
                video.playbackRate = {speed};
                return Math.abs(video.playbackRate - {speed}) < 0.01;
            }}
        ''')
        return True
    except (PwTimeoutError, PwError):
        return False


def video_pass(page: Page, monitor_selector: Optional[str] = None) -> bool:
    """
    播放视频并持续监视页面状态

    播放期间每隔 300ms 做一次轮询，同时检查四件事：
    1. 视频是否接近播放完毕
    2. 目标控件（如中途弹出的确认按钮）是否出现
    3. 通用弹窗检测：用户协议、脚本检测、违规提醒等
    4. 严格超时保护

    参数 monitor_selector 为 CSS 选择器，用于监视需要自动点击的控件。
    设置超时上限 = 视频时长/倍速 + 60s 硬上限余量。
    """
    try:
        page.wait_for_selector('video.vjs-tech', timeout=10000)
        video = page.locator('video.vjs-tech')

        # 静音
        video.evaluate('video => video.muted = true')
        logger.info("视频已静音")

        # 尝试播放
        try:
            video.evaluate('video => video.play()')
        except Exception:
            page.locator('video.vjs-tech').click(timeout=1000)
            video.evaluate('video => video.play()')

        target_speed = 2.0
        # 设置 2.0 倍速（JS 直接设 playbackRate）
        try:
            page.wait_for_selector('.vjs-control-bar', state='visible', timeout=5000)
            page.evaluate('() => { const v = document.querySelector("video.vjs-tech"); if(v) v.playbackRate = 2.0; }')
            logger.info("已设为 2.0 倍速")
        except Exception:
            logger.warning("控制栏未出现，使用 1x 播放")

        # 获取时长和倍速
        try:
            duration = page.evaluate('() => document.querySelector("video.vjs-tech").duration')
        except Exception:
            duration = 1800
        speed = target_speed
        if not (isinstance(duration, (int, float)) and duration > 0):
            duration = 1800
        if not (isinstance(speed, (int, float)) and speed > 0):
            speed = 1.0
        estimated_time = (duration / speed) + 60
        deadline = time.time() + estimated_time

        logger.info(f"开始播放，总时长 {duration:.1f}s，倍速 {speed}x")

        # 通用弹窗检测：需要自动关闭的弹窗关键词
        popup_keywords = [
            '禁止使用脚本', '脚本检测', '违规操作',
            '用户协议', '服务协议', '同意并继续',
            '第三方辅助工具', '检测到网络不稳定', '视频已暂停',
            '第三方工具',
        ]
        dismiss_keywords = [
            '我知道了', '确定', '确认', '同意',
            '关闭', '知道了',
        ]

        while True:
            if time.time() > deadline:
                logger.warning(f"播放超时（{estimated_time:.1f}s），强制结束")
                break

            # 检测视频进度
            try:
                is_finished = page.evaluate('''
                    () => {
                        const v = document.querySelector('video.vjs-tech');
                        if (!v) return true;
                        if (v.ended) return true;
                        const dur = v.duration;
                        return (typeof dur === 'number' && isFinite(dur) && dur > 0)
                            ? v.currentTime >= dur - 1
                            : false;
                    }
                ''')
                if is_finished:
                    logger.info("视频播放完成")
                    break
            except (PwError, Exception):
                # 页面已关闭/崩溃，视为视频结束
                logger.warning("视频页面已关闭，视为播放完成")
                break

            # 检查目标控件（原有）
            if monitor_selector:
                try:
                    target = page.locator(monitor_selector).first
                    if target.is_visible(timeout=200):
                        target.click(timeout=200)
                        logger.info("检测到目标控件并已点击")
                        time.sleep(0.5)
                        continue
                except PwTimeoutError:
                    pass
                except (PwError, Exception):
                    pass

            # 通用弹窗检测：遍历关键词找弹窗
            for kw in popup_keywords:
                try:
                    kw_el = page.locator(f'text={kw}').first
                    if kw_el.is_visible(timeout=200):
                        logger.warning(f'⚠️ 检测到弹窗: "{kw}"')
                        # 找关闭按钮
                        dismissed = False
                        for dk in dismiss_keywords:
                            try:
                                btn = page.locator(f'text={dk}').first
                                if btn.is_visible(timeout=200):
                                    btn.click(timeout=200)
                                    logger.info(f'✅ 已点击 "{dk}"')
                                    dismissed = True
                                    time.sleep(1)
                                    break
                            except:
                                continue
                        # 弹窗关闭后恢复播放（倍速保留浏览器原有设置，不重新设）
                        if dismissed:
                            try:
                                video = page.locator('video.vjs-tech')
                                video.evaluate('video => { video.muted = true; video.play(); }')
                                logger.info('↻ 已恢复播放')
                            except Exception:
                                pass
                        break
                except:
                    continue

            # 轮询间隔
            time.sleep(random.uniform(0.2, 0.6))

        logger.info("视频播放流程结束")
        return True
    except PwTimeoutError as e:
        logger.error(f"播放超时: {e}")
        # 不退出，让调用方处理
        return False
    except (PwError, Exception) as e:
        logger.warning(f"视频播放异常: {e}")
        return False


# ── 程序入口 ──────────────────────────────────────────────

if __name__ == "__main__":
    # ── 启动警告：告知用户账号封禁风险 ──
    print("=" * 60)
    print("  ⚠ 免责声明：本工具仅供自动化技术学习与研究")
    print("  使用自动化脚本操作 ewt360.com 可能违反该平台用户协议")
    print("  由此导致的账号封禁、学习记录异常等后果由使用者自行承担")
    print("  建议使用独立小号，勿在主账号上运行")
    print("=" * 60)
    print()

    with sync_playwright() as p:
        # 询问是否显示浏览器界面，y=显示（便于观察），n=后台静默运行
        show_browser = input("是否显示浏览器界面？(y/n，默认 y)：").strip().lower()
        headless_mode = show_browser not in ("", "y", "yes")

        # 查找同目录下的 Chromium（绿色版分发），找不到则用 Playwright 内置
        # 优先查找 exe 所在目录下的 chrome-win64/chrome.exe
        exe_dir = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
        chrome_path = os.path.join(exe_dir, "chrome-win64", "chrome.exe")
        chrome_args = [
            '--disable-blink-features=AutomationControlled',
            '--no-sandbox',
            '--disable-gpu',
            '--disable-software-rasterizer',
            '--disable-dev-shm-usage',
        ]
        if os.path.exists(chrome_path):
            logger.info(f"使用本地 Chromium：{chrome_path}")
            browser = p.chromium.launch(
                headless=headless_mode,
                executable_path=chrome_path,
                args=chrome_args,
            )
        else:
            # 回退到系统 Chrome（不用 Playwright 内置浏览器）
            browser = p.chromium.launch(
                headless=headless_mode,
                executable_path='/usr/bin/google-chrome',
                args=chrome_args,
            )
        context = browser.new_context()
        main_page = context.new_page()

        # 从环境变量读取凭据，避免硬编码；用户可在 .env 文件中配置
        # 优先级：os.environ > .env 文件 > 控制台输入（fallback）
        ewt_username = os.getenv("EWT_USERNAME") or input("请输入账号：")
        ewt_password = os.getenv("EWT_PASSWORD") or input("请输入密码：")
        ewt_url = os.getenv("EWT_URL", "https://www.ewt360.com/")

        # 空值校验：前置拦截空配置，避免无意义登录尝试
        if not ewt_username or not ewt_password:
            logger.error("账号或密码为空，请检查 .env 配置或手动输入")
            quit_ewt(1)

        # 登录（网络波动时最多重试 3 次），失败则安全退出
        try:
            retry_call(
                login,
                args=(main_page, ewt_username, ewt_password, ewt_url),
                max_attempts=3, delay=3.0, exceptions=(PwTimeoutError, Exception),
            )
        except Exception:
            logger.error("登录重试全部失败，退出程序")
            quit_ewt(1)

        # 跳转到作业详情页，失败则安全退出
        homework_url = os.getenv("EWT_HOMEWORK_URL",
                                 "https://teacher.ewt360.com/ewtbend/bend/index/index.html#/student/homework")
        try:
            detail_page = retry_call(
                turn_to_homework,
                args=(main_page, homework_url),
                max_attempts=3, delay=2.0, exceptions=(PwTimeoutError, Exception),
            )
        except Exception:
            logger.error("跳转作业详情页重试全部失败，退出程序")
            quit_ewt(1)

        detail_page.wait_for_load_state("networkidle")

        logger.info("提取任务列表中")
        schedules = get_schedule_list(page=detail_page)
        logger.info("提取任务列表完成")

        auto_choice(page=detail_page, schedule=schedules)

        # 所有任务执行完毕后退出
        logger.info("🎉 全部淦完，收工！")
        quit_ewt(0)
