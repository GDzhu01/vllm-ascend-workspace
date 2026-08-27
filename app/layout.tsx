import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'NPU Fleet Monitor',
  description: '本地部署的 Ascend NPU 服务器实时监控与历史报表',
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
