"""Email notification service.

Provides email sending capabilities for:
- Daily worker reports
- Alert notifications
- Test emails
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Dict, List, Any, Optional
import logging
import os

log = logging.getLogger(__name__)


class EmailService:
    """Email service for sending notifications."""
    
    def __init__(self):
        """Initialize email service with configuration from environment."""
        self.smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.from_email = os.getenv("FROM_EMAIL", self.smtp_user)
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
            msg['From'] = f"{self.from_name} <{self.from_email}>"
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
