import WhiteboardCanvas from './WhiteboardCanvas'
import OpenPM from '../OpenPM/OpenPM'
import Timeline from '../Timeline/Timeline'
import Story from '../Story/Story'
import CommitChipRail from './CommitChipRail'

export default function Whiteboard({
  activeTool, setActiveTool,
  activeView, setActiveView,
  storyNonce = 0,
  canvasState, canvasDispatch,
  onLoadSelfMap,
  onGenerateFromRepo,
  onLoadSketchDemo,
  onLoadSaasDemo,
  onExecute,
  executing,
  repoName = '',
  // In-place nesting (Step 16)
  archGraph,
  webxrBadges = null,
  expandedIds,
  onToggleExpand,
  onSelectArchEntity,
  archSel,
  onExpandModule,
  flowMode = 'story',
  failFocus = null,
  onFocusFile,
  flowLens = null,
  repairPhase = null,
  onExitFlowLens,
  onRegenFlowLens,
  onOpenEditor = null,
  openFlowFns = null,
  story = null,
  runNodeStates,
  runEdgeStates,
  watchBoxIds = null,
  watchConnected = false,
  hydrating = false,
  watchFocus = null,
  liveFollow = true,
  onToggleLiveFollow,
  spotlight = null,
  onClearSpotlight,
  onSpotlightCommit,
  gitCommits,
  onSelectCommit,
  worktreeDirty = false,
  worktreeCount = 0,
  onReviewChanges,
  reviewActive = false,
  episodes = [],
  outsideBucket = null,
  onSpotlightEpisode,
  onSpotlightFiles,
  activeEpisodeId = null,
  onSpotlightOutside,
  outsideActive = false,
  // Story props
  onSelectConcept,
  highlightTags = null,
  // PM props
  tasks, pmDispatch,
  designEvents, onTaskEvent,
  selectedTaskId, setSelectedTaskId,
  setPanelMode,
}) {
  if (activeView === 'timeline') {
    return (
      <Timeline
        designEvents={designEvents}
        canvasState={canvasState}
        canvasDispatch={canvasDispatch}
        setActiveView={setActiveView}
        setPanelMode={setPanelMode}
        tasks={tasks}
        setSelectedTaskId={setSelectedTaskId}
        gitCommits={gitCommits}
        onSelectCommit={onSelectCommit}
      />
    )
  }

  if (activeView === 'pm') {
    return (
      <OpenPM
        tasks={tasks}
        pmDispatch={pmDispatch}
        canvasState={canvasState}
        canvasDispatch={canvasDispatch}
        setActiveView={setActiveView}
        setPanelMode={setPanelMode}
        selectedTaskId={selectedTaskId}
        setSelectedTaskId={setSelectedTaskId}
        onTaskEvent={onTaskEvent}
        onSpotlightCommit={onSpotlightCommit}
        onFocusFile={onFocusFile}
        highlightTags={highlightTags}
        onClearHighlight={onSelectConcept ? () => onSelectConcept(null) : null}
      />
    )
  }

  if (activeView === 'story') {
    return (
      <Story
        episodes={episodes}
        storyNonce={storyNonce}
        onSpotlightEpisode={onSpotlightEpisode}
        onSpotlightCommit={onSpotlightCommit}
        onSpotlightFiles={onSpotlightFiles}
        onSelectConcept={onSelectConcept}
        setActiveView={setActiveView}
      />
    )
  }

  return (
    <div className="wb-shell">
      <div className="arch-header">
        <span className="arch-header-title">openarchitect</span>
        {/* Canvas-native commit lens (Step 37a Slice 3) — click a chip to see
            what changed; never replaces the Timeline tab. */}
        <CommitChipRail
          worktreeDirty={worktreeDirty}
          worktreeCount={worktreeCount}
          onReviewChanges={onReviewChanges}
          reviewActive={reviewActive}
          episodes={episodes}
          outsideBucket={outsideBucket}
          onSpotlightEpisode={onSpotlightEpisode}
          activeEpisodeId={activeEpisodeId}
          onSpotlightOutside={onSpotlightOutside}
          outsideActive={outsideActive}
        />
      </div>
      <div className="wb-body">
        <WhiteboardCanvas
          activeTool={activeTool}
          setActiveTool={setActiveTool}
          state={canvasState}
          dispatch={canvasDispatch}
          onLoadSelfMap={onLoadSelfMap}
          onGenerateFromRepo={onGenerateFromRepo}
          onLoadSketchDemo={onLoadSketchDemo}
          onLoadSaasDemo={onLoadSaasDemo}
          onExecute={onExecute}
          executing={executing}
          repoName={repoName}
          archGraph={archGraph}
          webxrBadges={webxrBadges}
          expandedIds={expandedIds}
          onToggleExpand={onToggleExpand}
          onSelectArchEntity={onSelectArchEntity}
          archSel={archSel}
          onExpandModule={onExpandModule}
          flowMode={flowMode}
          story={story}
          failFocus={failFocus}
          flowLens={flowLens}
          repairPhase={repairPhase}
          onExitFlowLens={onExitFlowLens}
          onRegenFlowLens={onRegenFlowLens}
          onOpenEditor={onOpenEditor}
          openFlowFns={openFlowFns}
          runNodeStates={runNodeStates}
          runEdgeStates={runEdgeStates}
          watchBoxIds={watchBoxIds}
          watchConnected={watchConnected}
          hydrating={hydrating}
          watchFocus={watchFocus}
          liveFollow={liveFollow}
          onToggleLiveFollow={onToggleLiveFollow}
          spotlight={spotlight}
          onClearSpotlight={onClearSpotlight}
        />
      </div>
    </div>
  )
}
