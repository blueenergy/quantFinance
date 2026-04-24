"""Email notification service.

Provides email sending capabilities for:
- Daily worker reports
- Alert notifications
- Test emails
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from datetime import datetime
from typing import Dict, List, Any, Optional
import logging
import os

log = logging.getLogger(__name__)


class EmailService:
    """Email service for sending notifications."""
    
    def __init__(self):
        """Initialize email service with configuration from environment."""
        self.smtp_host = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USERNAME", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.from_email = os.getenv("SMTP_FROM_EMAIL", self.smtp_user)
        self.from_name = os.getenv("FROM_NAME", "悟空量化")
        
        self.enabled = bool(self.smtp_user and self.smtp_password)
        
        if not self.enabled:
            log.warning("Email service not configured - emails will not be sent")
    
    def send_email(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        text_body: Optional[str] = None
    ) -> bool:
        """Send an email.
        
        Args:
            to_email: Recipient email address
            subject: Email subject
            html_body: HTML email body
            text_body: Plain text fallback (optional)
            
        Returns:
            bool: True if successful
        """
        if not self.enabled:
            log.warning(f"Email service disabled - skipping email to {to_email}")
            return False
        
        try:
            # Create message
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = formataddr((self.from_name, self.from_email))
            msg['To'] = to_email
            
            # Add text and HTML parts
            if text_body:
                text_part = MIMEText(text_body, 'plain', 'utf-8')
                msg.attach(text_part)
            
            html_part = MIMEText(html_body, 'html', 'utf-8')
            msg.attach(html_part)
            
            # Send email
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)
            
            log.info(f"Email sent successfully to {to_email}")
            return True
            
        except Exception as e:
            log.error(f"Failed to send email to {to_email}: {e}")
            return False
    
    def send_daily_report(
        self,
        to_email: str,
        username: str,
        report_data: Dict[str, Any]
    ) -> bool:
        """Send daily worker activity report.
        
        Args:
            to_email: Recipient email
            username: User's name
            report_data: Report data containing:
                - date: Report date
                - workers: List of worker summaries
                - total_scans: Total scans
                - total_signals: Total signals
                - total_trades: Total trades
        """
        date = report_data.get("date", datetime.now().strftime("%Y-%m-%d"))
        total_scans = report_data.get("total_scans", 0)
        total_signals = report_data.get("total_signals", 0)
        total_trades = report_data.get("total_trades", 0)
        workers = report_data.get("workers", [])
        
        # Build HTML email
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
        .container {{ max-width: 600px; margin: 0 auto; background: white; border-radius: 8px; overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; text-align: center; }}
        .header h1 {{ margin: 0; font-size: 24px; }}
        .content {{ padding: 30px; }}
        .summary {{ display: flex; justify-content: space-around; margin: 20px 0; }}
        .stat {{ text-align: center; }}
        .stat-value {{ font-size: 32px; font-weight: bold; color: #667eea; }}
        .stat-label {{ font-size: 14px; color: #666; margin-top: 5px; }}
        .workers {{ margin-top: 30px; }}
        .worker-item {{ background: #f8f9fa; padding: 15px; margin-bottom: 10px; border-radius: 6px; }}
        .worker-name {{ font-weight: bold; color: #333; }}
        .worker-stats {{ font-size: 14px; color: #666; margin-top: 8px; }}
        .footer {{ background: #f8f9fa; padding: 20px; text-align: center; font-size: 12px; color: #999; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🤖 Worker 日报</h1>
            <p>{date}</p>
        </div>
        
        <div class="content">
            <p>Hi {username},</p>
            <p>以下是您的 Worker 今日工作汇总：</p>
            
            <div class="summary">
                <div class="stat">
                    <div class="stat-value">{total_scans}</div>
                    <div class="stat-label">市场扫描</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{total_signals}</div>
                    <div class="stat-label">信号生成</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{total_trades}</div>
                    <div class="stat-label">交易执行</div>
                </div>
            </div>
            
            <div class="workers">
                <h3>Worker 明细</h3>
"""
        
        # Add worker details
        for worker in workers:
            html += f"""
                <div class="worker-item">
                    <div class="worker-name">{worker.get('symbol', 'N/A')} - {worker.get('strategy', 'N/A')}</div>
                    <div class="worker-stats">
                        扫描: {worker.get('scans', 0)} 次 | 
                        信号: {worker.get('signals', 0)} 个 | 
                        运行时长: {worker.get('uptime', 'N/A')}
                    </div>
                </div>
"""
        
        html += """
            </div>
            
            <p style="margin-top: 30px;">
                <a href="{dashboard_url}" style="display: inline-block; background: #667eea; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px;">
                    查看完整报告
                </a>
            </p>
        </div>
        
        <div class="footer">
            <p>悟空量化金融智能助手</p>
            <p>本邮件由系统自动发送，请勿回复</p>
        </div>
    </div>
</body>
</html>
"""
        
        # Plain text fallback
        text = f"""
Worker 日报 - {date}

Hi {username},

今日工作汇总:
- 市场扫描: {total_scans} 次
- 信号生成: {total_signals} 个
- 交易执行: {total_trades} 笔

Worker 明细:
"""
        for worker in workers:
            text += f"\n- {worker.get('symbol')} ({worker.get('strategy')}): {worker.get('scans', 0)} 次扫描, {worker.get('signals', 0)} 个信号"
        
        text += "\n\n悟空量化金融智能助手"
        
        return self.send_email(
            to_email=to_email,
            subject=f"📊 Worker 日报 - {date}",
            html_body=html,
            text_body=text
        )
    
    def send_worker_timeout_alert(
        self,
        to_email: str,
        username: str,
        worker_info: Dict[str, Any]
    ) -> bool:
        """Send worker timeout alert.
        
        Args:
            to_email: Recipient email
            username: User's name
            worker_info: Worker info (symbol, strategy, last_heartbeat)
        """
        symbol = worker_info.get("symbol", "Unknown")
        strategy = worker_info.get("strategy", "Unknown")
        last_heartbeat = worker_info.get("last_heartbeat_time", "Unknown")
        
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
        .container {{ max-width: 600px; margin: 0 auto; background: white; border-radius: 8px; overflow: hidden; }}
        .header {{ background: #e74c3c; color: white; padding: 30px; text-align: center; }}
        .content {{ padding: 30px; }}
        .alert {{ background: #fff3cd; border-left: 4px solid #ffc107; padding: 15px; margin: 20px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>⚠️ Worker 超时警告</h1>
        </div>
        <div class="content">
            <p>Hi {username},</p>
            <div class="alert">
                <strong>Worker 已超过 3 分钟未响应</strong>
                <p>股票: {symbol}<br>
                策略: {strategy}<br>
                最后心跳: {last_heartbeat}</p>
            </div>
            <p>请检查 Worker 运行状态。</p>
        </div>
    </div>
</body>
</html>
"""
        
        return self.send_email(
            to_email=to_email,
            subject=f"⚠️ Worker 超时警告 - {symbol}",
            html_body=html
        )
    
    def send_signal_notification(
        self,
        to_email: str,
        username: str,
        signal_info: Dict[str, Any]
    ) -> bool:
        """Send signal generation notification.
        
        Args:
            to_email: Recipient email
            username: User's name
            signal_info: Signal info (symbol, action, price, strategy)
        """
        symbol = signal_info.get("symbol", "Unknown")
        action = signal_info.get("action", "Unknown")
        price = signal_info.get("price", 0)
        strategy = signal_info.get("strategy", "Unknown")
        
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
        .container {{ max-width: 600px; margin: 0 auto; background: white; border-radius: 8px; overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: white; padding: 30px; text-align: center; }}
        .content {{ padding: 30px; }}
        .signal {{ background: #e3f2fd; padding: 20px; border-radius: 6px; margin: 20px 0; }}
        .signal-detail {{ margin: 10px 0; font-size: 16px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>⚡ 新信号生成</h1>
        </div>
        <div class="content">
            <p>Hi {username},</p>
            <p>Worker 发现新的交易机会：</p>
            <div class="signal">
                <div class="signal-detail"><strong>股票:</strong> {symbol}</div>
                <div class="signal-detail"><strong>操作:</strong> {action}</div>
                <div class="signal-detail"><strong>价格:</strong> ¥{price}</div>
                <div class="signal-detail"><strong>策略:</strong> {strategy}</div>
            </div>
            <p>请登录系统查看详情。</p>
        </div>
    </div>
</body>
</html>
"""
        
        return self.send_email(
            to_email=to_email,
            subject=f"⚡ 新信号 - {symbol} {action}",
            html_body=html
        )


    def send_account_approved_email(
        self,
        to_email: str,
        username: str,
        login_url: str
    ) -> bool:
        """Send account approval notification to user.

        Args:
            to_email: Recipient email
            username: User's username
            login_url: URL to login page
        """
        html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body {{ margin: 0; padding: 0; background: #f0f2f5; font-family: 'PingFang SC', 'Helvetica Neue', Arial, sans-serif; }}
    .wrapper {{ max-width: 620px; margin: 30px auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 24px rgba(0,0,0,0.10); }}
    .header {{ background: linear-gradient(135deg, #667eea 0%, #f6d365 100%); padding: 42px 30px 32px; text-align: center; }}
    .header-icon {{ font-size: 48px; line-height: 1; margin-bottom: 10px; }}
    .header h1 {{ margin: 0 0 6px; color: #fff; font-size: 22px; font-weight: 700; letter-spacing: 1px; text-shadow: 0 1px 4px rgba(0,0,0,0.18); }}
    .header p {{ margin: 0; color: rgba(255,255,255,0.90); font-size: 14px; }}
    .content {{ padding: 32px 30px 24px; }}
    .greeting {{ font-size: 15px; color: #333; margin-bottom: 6px; }}
    .intro {{ font-size: 14px; color: #555; margin-bottom: 24px; line-height: 1.7; }}
    .section-title {{ font-size: 13px; font-weight: 600; color: #888; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 14px; }}
    .feature-grid {{ display: table; width: 100%; border-collapse: separate; border-spacing: 0 10px; }}
    .feature-item {{ display: table-row; }}
    .feature-icon-cell {{ display: table-cell; width: 52px; vertical-align: top; padding: 14px 0 14px 16px; background: #f7f8fc; border-radius: 10px 0 0 10px; border-left: 3px solid #667eea; }}
    .feature-icon {{ font-size: 24px; line-height: 1; }}
    .feature-body-cell {{ display: table-cell; vertical-align: top; padding: 14px 16px; background: #f7f8fc; border-radius: 0 10px 10px 0; }}
    .feature-name {{ font-size: 14px; font-weight: 700; color: #333; margin-bottom: 3px; }}
    .feature-desc {{ font-size: 12px; color: #777; line-height: 1.6; }}
    .spacer {{ height: 10px; }}
    .cta-wrap {{ text-align: center; margin: 28px 0 8px; }}
    .cta-btn {{ display: inline-block; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #ffffff !important; text-decoration: none; padding: 14px 40px; border-radius: 30px; font-size: 15px; font-weight: 700; letter-spacing: 0.5px; box-shadow: 0 4px 14px rgba(102,126,234,0.40); }}
    .footer {{ background: #f7f8fc; padding: 18px 30px; text-align: center; font-size: 12px; color: #aaa; line-height: 1.7; }}
    .footer a {{ color: #667eea; text-decoration: none; }}
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="header">
      <div class="header-icon">🔥</div>
      <h1>悟空量化金融智能助手</h1>
      <p>您的账号已通过审批，欢迎加入！</p>
    </div>
    <div class="content">
      <p class="greeting">Hi <strong>{username}</strong>，</p>
      <p class="intro">
        恭喜您，您的悟空量化账号已正式开通！<br>
        以下是平台五大核心功能导览，助您快速上手，抓住每一个量化机会。
      </p>

      <p class="section-title">平台核心功能</p>

      <div class="feature-grid">
        <div class="feature-item">
          <div class="feature-icon-cell"><div class="feature-icon">🌍</div></div>
          <div class="feature-body-cell">
            <div class="feature-name">隔夜全球市场联动分析</div>
            <div class="feature-desc">盘前掌握美股、港股、期货隔夜动向，AI 研判 A 股开盘方向，比市场早一步布局。</div>
          </div>
        </div>
        <div class="spacer"></div>
        <div class="feature-item">
          <div class="feature-icon-cell"><div class="feature-icon">📢</div></div>
          <div class="feature-body-cell">
            <div class="feature-name">AI 大盘分析公告栏</div>
            <div class="feature-desc">AI 实时解读大盘走势与市场情绪，盘中盘后关键信号一目了然，永远不错过重要节点。</div>
          </div>
        </div>
        <div class="spacer"></div>
        <div class="feature-item">
          <div class="feature-icon-cell"><div class="feature-icon">🏆</div></div>
          <div class="feature-body-cell">
            <div class="feature-name">股票金榜排名</div>
            <div class="feature-desc">多维度量化评分驱动的选股排行，每日精选最强潜力股，让好股票主动找到您。</div>
          </div>
        </div>
        <div class="spacer"></div>
        <div class="feature-item">
          <div class="feature-icon-cell"><div class="feature-icon">📊</div></div>
          <div class="feature-body-cell">
            <div class="feature-name">连板天梯</div>
            <div class="feature-desc">实时追踪涨停连板个股，精准捕捉热点轮动与强势趋势延续机会，跟上主线节奏。</div>
          </div>
        </div>
        <div class="spacer"></div>
        <div class="feature-item">
          <div class="feature-icon-cell"><div class="feature-icon">☯</div></div>
          <div class="feature-body-cell">
            <div class="feature-name">大盘阴阳谱</div>
            <div class="feature-desc">独家市场多空强弱图谱，直观呈现资金博弈格局与趋势拐点信号，读懂市场内在节律。</div>
          </div>
        </div>
      </div>

      <div class="cta-wrap">
        <a href="{login_url}" class="cta-btn">🚀 立即登录，开始量化之旅</a>
      </div>
    </div>
    <div class="footer">
      悟空量化金融智能助手 &nbsp;·&nbsp; 本邮件由系统自动发送，请勿回复<br>
      如有问题请联系管理员
    </div>
  </div>
</body>
</html>"""

        text = f"""Hi {username}，

恭喜您，您的悟空量化账号已正式开通！

平台核心功能导览：

🌍 隔夜全球市场联动分析
   盘前掌握美股、港股、期货隔夜动向，AI 研判 A 股开盘方向。

📢 AI 大盘分析公告栏
   AI 实时解读大盘走势与市场情绪，盘中盘后关键信号一目了然。

🏆 股票金榜排名
   多维度量化评分驱动的选股排行，每日精选最强潜力股。

📊 连板天梯
   实时追踪涨停连板个股，精准捕捉热点轮动与强势趋势延续机会。

☯ 大盘阴阳谱
   独家市场多空强弱图谱，直观呈现资金博弈格局与趋势拐点信号。

立即登录：{login_url}

---
悟空量化金融智能助手 · 本邮件由系统自动发送，请勿回复"""

        return self.send_email(
            to_email=to_email,
            subject="🎉 您的悟空量化账号已开通 — 立即开始量化之旅",
            html_body=html,
            text_body=text
        )

    def send_self_activation_email(
        self,
        to_email: str,
        username: str,
        activation_url: str
    ) -> bool:
        """新用户注册后：邮件内链接在前端 /activate 完成账户开通（与管理员审批可并存，幂等）。"""
        html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body {{ margin: 0; padding: 0; background: #f0f2f5; font-family: 'PingFang SC', 'Helvetica Neue', Arial, sans-serif; }}
    .wrapper {{ max-width: 620px; margin: 30px auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 24px rgba(0,0,0,0.10); }}
    .header {{ background: linear-gradient(135deg, #6b8ef7 0%, #a78bfa 100%); padding: 40px 30px 32px; text-align: center; }}
    .header h1 {{ margin: 0 0 6px; color: #fff; font-size: 22px; font-weight: 700; }}
    .header p {{ margin: 0; color: rgba(255,255,255,0.9); font-size: 14px; }}
    .content {{ padding: 32px 30px; }}
    .greeting {{ font-size: 15px; color: #333; margin-bottom: 12px; }}
    .intro {{ font-size: 14px; color: #555; line-height: 1.7; margin-bottom: 24px; }}
    .cta-wrap {{ text-align: center; margin: 8px 0; }}
    .cta-btn {{ display: inline-block; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #fff !important; text-decoration: none; padding: 14px 36px; border-radius: 30px; font-size: 15px; font-weight: 600; box-shadow: 0 4px 14px rgba(102,126,234,0.35); }}
    .tip {{ font-size: 12px; color: #999; margin-top: 20px; line-height: 1.6; }}
    .footer {{ background: #f7f8fc; padding: 16px 30px; text-align: center; font-size: 12px; color: #aaa; }}
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="header">
      <h1>请确认您的悟空量化注册</h1>
      <p>点击按钮即可完成开通，无需等待人工审批</p>
    </div>
    <div class="content">
      <p class="greeting">Hi <strong>{username}</strong>，</p>
      <p class="intro">感谢您注册。点击下方按钮确认邮箱并开通账户。管理员也可能为您审批；若已开通，再次打开本邮件中的链接时我们会提示您直接登录。</p>
      <div class="cta-wrap">
        <a href="{activation_url}" class="cta-btn">完成开通</a>
      </div>
      <p class="tip">若按钮无法点击，请将以下链接复制到浏览器地址栏打开：<br>{activation_url}</p>
    </div>
    <div class="footer">本邮件由系统自动发出，请勿直接回复</div>
  </div>
</body>
</html>"""

        text = f"""Hi {username}，

感谢您注册悟空量化。请访问以下链接完成账户开通（也可等待管理员人工审批后登录）：

{activation_url}

若链接已失效，可在登录页通过「重发开通邮件」再次获取（若本功能已在前端提供）。

— 悟空量化"""

        return self.send_email(
            to_email=to_email,
            subject="请确认并开通您的悟空量化账户",
            html_body=html,
            text_body=text
        )

    def send_never_logged_in_reminder(
        self,
        to_email: str,
        username: str,
        login_url: str
    ) -> bool:
        """Send login reminder to an approved-but-never-logged-in user.

        Args:
            to_email: Recipient email
            username: User's username
            login_url: URL to login page
        """
        html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body {{ margin: 0; padding: 0; background: #f0f2f5; font-family: 'PingFang SC', 'Helvetica Neue', Arial, sans-serif; }}
    .wrapper {{ max-width: 620px; margin: 30px auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 24px rgba(0,0,0,0.10); }}
    .header {{ background: linear-gradient(135deg, #f6d365 0%, #fd7043 100%); padding: 40px 30px 30px; text-align: center; }}
    .header-icon {{ font-size: 48px; line-height: 1; margin-bottom: 10px; }}
    .header h1 {{ margin: 0 0 6px; color: #fff; font-size: 22px; font-weight: 700; letter-spacing: 1px; text-shadow: 0 1px 4px rgba(0,0,0,0.18); }}
    .header p {{ margin: 0; color: rgba(255,255,255,0.90); font-size: 14px; }}
    .content {{ padding: 32px 30px 24px; }}
    .greeting {{ font-size: 15px; color: #333; margin-bottom: 6px; }}
    .intro {{ font-size: 14px; color: #555; margin-bottom: 24px; line-height: 1.7; }}
    .highlight-box {{ background: linear-gradient(135deg, #fff8e1 0%, #fff3e0 100%); border-left: 4px solid #fd7043; border-radius: 0 8px 8px 0; padding: 16px 20px; margin-bottom: 24px; font-size: 13px; color: #555; line-height: 1.8; }}
    .highlight-box strong {{ color: #e65100; }}
    .cta-wrap {{ text-align: center; margin: 28px 0 8px; }}
    .cta-btn {{ display: inline-block; background: linear-gradient(135deg, #f6d365 0%, #fd7043 100%); color: #ffffff !important; text-decoration: none; padding: 14px 40px; border-radius: 30px; font-size: 15px; font-weight: 700; letter-spacing: 0.5px; box-shadow: 0 4px 14px rgba(253,112,67,0.40); }}
    .footer {{ background: #f7f8fc; padding: 18px 30px; text-align: center; font-size: 12px; color: #aaa; line-height: 1.7; }}
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="header">
      <div class="header-icon">👋</div>
      <h1>悟空量化金融智能助手</h1>
      <p>您的账号还在等待您的第一次登录</p>
    </div>
    <div class="content">
      <p class="greeting">Hi <strong>{username}</strong>，</p>
      <p class="intro">
        您的悟空量化账号已通过审批并开通，但我们注意到您还<strong>从未登录过</strong>。<br>
        量化市场每天都在变化，不想错过每日盘前的全球联动分析、AI 大盘公告栏与金榜选股吧？
      </p>
      <div class="highlight-box">
        <strong>现在登录，您可以立即体验：</strong><br>
        🌍 隔夜全球市场联动分析，盘前占据先机<br>
        📢 AI 大盘分析公告栏，盘中盘后实时播报<br>
        🏆 股票金榜排名，量化评分筛出强势股<br>
        📊 连板天梯 &amp; ☯ 大盘阴阳谱，读懂市场节律
      </div>
      <div class="cta-wrap">
        <a href="{login_url}" class="cta-btn">🚀 立即登录</a>
      </div>
    </div>
    <div class="footer">
      悟空量化金融智能助手 &nbsp;·&nbsp; 本邮件由管理员触发发送，请勿回复
    </div>
  </div>
</body>
</html>"""

        text = f"""Hi {username}，

您的悟空量化账号已开通，但我们注意到您还从未登录过。

现在登录，您可以立即体验：
🌍 隔夜全球市场联动分析，盘前占据先机
📢 AI 大盘分析公告栏，盘中盘后实时播报
🏆 股票金榜排名，量化评分筛出强势股
📊 连板天梯 & ☯ 大盘阴阳谱，读懂市场节律

立即登录：{login_url}

---
悟空量化金融智能助手 · 本邮件由管理员触发发送，请勿回复"""

        return self.send_email(
            to_email=to_email,
            subject="👋 您的悟空量化账号还未激活 — 快来探索量化世界",
            html_body=html,
            text_body=text
        )


# Global email service instance
email_service = EmailService()


# Convenience functions
def send_daily_report(to_email: str, username: str, report_data: Dict[str, Any]) -> bool:
    """Send daily report email."""
    return email_service.send_daily_report(to_email, username, report_data)


def send_worker_timeout_alert(to_email: str, username: str, worker_info: Dict[str, Any]) -> bool:
    """Send worker timeout alert."""
    return email_service.send_worker_timeout_alert(to_email, username, worker_info)


def send_signal_notification(to_email: str, username: str, signal_info: Dict[str, Any]) -> bool:
    """Send signal notification."""
    return email_service.send_signal_notification(to_email, username, signal_info)


def send_test_email(to_email: str, username: str) -> bool:
    """Send test email."""
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
</head>
<body>
    <h2>测试邮件</h2>
    <p>Hi {username},</p>
    <p>这是一封测试邮件，用于验证邮件配置是否正确。</p>
    <p>如果您收到这封邮件，说明邮件服务配置成功！</p>
    <br>
    <p>悟空量化金融智能助手</p>
</body>
</html>
"""
    return email_service.send_email(
        to_email=to_email,
        subject="✅ 测试邮件 - 悟空量化",
        html_body=html,
        text_body=f"Hi {username},\n\n这是一封测试邮件。\n\n悟空量化金融智能助手"
    )
