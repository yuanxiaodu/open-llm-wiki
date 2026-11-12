// 对话空态：动作卡。
//
// 建设权限开着的账号：三张卡 = 知识库的三件事（问 / 喂 / 修）。
// 只有使用权限的账号：换成三张只读动作（问 / 找 / 划重点）——不能摄入、不能改页，
// 所以「整理一份资料」和「体检知识库」都不该出现。
//
// 卡片内容来自 app.toml（[ui].build_actions / readonly_actions），由 /api/site 下发；
// ChatWelcome 只负责渲染与分发点击，不在组件里写死文案。
//
// 两个入口共用：没选会话的首页、选了空会话时的空消息流。

import { useSiteConfig } from "../site.ts";
import {
  DEFAULT_BUILD_ACTIONS,
  DEFAULT_READONLY_ACTIONS,
  type WelcomeAction,
  type WelcomeActionKey,
} from "../welcome.ts";

export type { WelcomeAction, WelcomeActionKey };

/** 有建设权限的默认动作（配置取不到时兜底）。 */
export const WELCOME_ACTIONS: readonly WelcomeAction[] = DEFAULT_BUILD_ACTIONS;

/** 只有使用权限的默认动作。 */
export const READONLY_ACTIONS: readonly WelcomeAction[] = DEFAULT_READONLY_ACTIONS;

/** 有建设权限时的三句默认例子，原样导出：测试与别处要引同一份文案。 */
export const SUGGESTIONS = DEFAULT_BUILD_ACTIONS.map((action) => action.example);

/** 只读账号的三句默认例子。 */
export const READONLY_SUGGESTIONS = DEFAULT_READONLY_ACTIONS.map((action) => action.example);

export interface ChatWelcomeProps {
  /** 点「问」「找」「划重点」：把例子填进输入框（不发送、不建会话）。 */
  onSuggest: (text: string) => void;
  /** 点「整理」：打开文件选择器（选择行为在 Composer）。只读账号不会看到这张卡。 */
  onFeed: () => void;
  /** 点「体检」：把体检指令填进输入框（不发送）。只读账号不会看到这张卡。 */
  onDoctor: () => void;
  /**
   * 这个账号能不能摄入材料。false = 只有使用权限：整组换成只读动作，
   * 「整理一份资料」「体检知识库」都不渲染。必填，由调用方交（漏传就编不过）。
   */
  canFeed: boolean;
}

export function ChatWelcome({ onSuggest, onFeed, onDoctor, canFeed }: ChatWelcomeProps) {
  const { welcome } = useSiteConfig();
  const actions = canFeed ? welcome.build : welcome.readonly;
  return (
    <div className="welcome chat-welcome">
      <h2>从知识库开始</h2>
      <div className="action-row" aria-label="可以这样开始">
        {actions.map((action) => (
          <button
            type="button"
            className="action-card"
            key={action.key}
            onClick={() => {
              if (action.key === "feed") onFeed();
              else if (action.key === "lint") onDoctor();
              else onSuggest(action.example);
            }}
          >
            <span className="ac-icon" aria-hidden="true">
              {action.icon}
            </span>
            <span className="ac-title">{action.title}</span>
            <span className="ac-example">{action.example}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
