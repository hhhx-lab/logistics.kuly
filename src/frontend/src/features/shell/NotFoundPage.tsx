// 未知路径：说明发生了什么并给出可用入口，不静默跳转。
import { useNavigate } from "react-router-dom";
import { EmptyState } from "../../components/Status";

export function NotFoundPage() {
  const navigate = useNavigate();
  return (
    <div className="flex h-full items-center justify-center">
      <EmptyState
        icon="question"
        title="这个地址不存在"
        description="链接可能已过期，或对象已被删除。"
        action={
          <div className="flex gap-2">
            <button type="button" className="btn btn-primary" onClick={() => navigate("/conversation")}>
              回到对话
            </button>
            <button type="button" className="btn btn-secondary" onClick={() => navigate("/documents")}>
              打开单据管理台
            </button>
          </div>
        }
      />
    </div>
  );
}
