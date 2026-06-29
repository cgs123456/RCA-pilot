/**
 * 占位页面 — Phase 2 实现
 */
import { Card, CardBody } from '@/components/ui/Card';
import type { LucideIcon } from 'lucide-react';
import { Construction } from 'lucide-react';

export function PlaceholderPage({ title, description, icon: Icon }: {
  title: string;
  description: string;
  icon?: LucideIcon;
}) {
  const IconComp = Icon || Construction;
  return (
    <div className="p-6">
      <Card className="max-w-2xl mx-auto">
        <CardBody className="text-center py-16">
          <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-brand-50 flex items-center justify-center">
            <IconComp className="w-8 h-8 text-brand-500" />
          </div>
          <h2 className="text-xl font-bold text-ink-900 mb-2">{title}</h2>
          <p className="text-sm text-ink-500 max-w-md mx-auto">{description}</p>
          <div className="inline-block mt-4 px-3 py-1 bg-warning-50 text-warning-600 text-xs rounded">
            Phase 2 实现
          </div>
        </CardBody>
      </Card>
    </div>
  );
}
