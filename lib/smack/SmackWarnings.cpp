#include "llvm/IR/DebugInfoMetadata.h"
#include "llvm/IR/DebugLoc.h"
#include "llvm/Support/raw_ostream.h"

#include "smack/SmackOptions.h"
#include "smack/SmackWarnings.h"

#include <algorithm>
#include <set>

namespace smack {
using namespace llvm;

std::string buildDebugInfo(const Instruction *i) {
  std::stringstream ss;
  if (i && i->getMetadata("dbg")) {
    const DebugLoc DL = i->getDebugLoc();
    auto *scope = cast<DIScope>(DL.getScope());

    ss << scope->getFilename().str() << ":" << DL.getLine() << ":"
       << DL.getCol() << ": ";
  }
  return ss.str();
}

bool SmackWarnings::isSufficientWarningLevel(WarningLevel level) {
  return SmackOptions::WarningLevel >= level;
}

SmackWarnings::UnsetFlagsT
SmackWarnings::getUnsetFlags(RequiredFlagsT requiredFlags) {
  UnsetFlagsT ret;
  std::copy_if(requiredFlags.begin(), requiredFlags.end(),
               std::inserter(ret, ret.begin()),
               [](FlagT *flag) { return !*flag; });
  return ret;
}

bool SmackWarnings::isSatisfied(RequiredFlagsT requiredFlags,
                                FlagRelation rel) {
  auto unsetFlags = getUnsetFlags(requiredFlags);
  return rel == FlagRelation::And ? unsetFlags.empty()
                                  : unsetFlags.size() < requiredFlags.size();
}

std::string SmackWarnings::getFlagStr(UnsetFlagsT flags) {
  std::string ret = "{ ";
  for (auto f : flags) {
    if (f->ArgStr.str() == "bit-precise")
      ret += ("--integer-encoding=bit-vector ");
    else
      ret += ("--" + f->ArgStr.str() + " ");
  }
  return ret + "}";
}

void SmackWarnings::warnUnModeled(std::string unmodeledOpName, Block *currBlock,
                                  const Instruction *i) {
  warnImprecise("unmodeled operation " + unmodeledOpName, "", {}, currBlock, i);
}

void SmackWarnings::warnIfIncomplete(std::string name, UnsetFlagsT unsetFlags,
                                     Block *currBlock, const Instruction *i,
                                     FlagRelation rel) {
  warnImprecise(name, "over-approximating", unsetFlags, currBlock, i, rel);
}

void SmackWarnings::warnIfIncomplete(std::string name,
                                     RequiredFlagsT requiredFlags,
                                     Block *currBlock, const Instruction *i,
                                     FlagRelation rel) {
  if (!isSatisfied(requiredFlags, rel))
    warnIfIncomplete(name, getUnsetFlags(requiredFlags), currBlock, i, rel);
}

void SmackWarnings::warnImprecise(std::string name, std::string description,
                                  UnsetFlagsT unsetFlags, Block *currBlock,
                                  const Instruction *i, FlagRelation rel) {
  if (!isSufficientWarningLevel(WarningLevel::Imprecise))
    return;
  std::string beginning = std::string("llvm2bpl: ") + buildDebugInfo(i);
  std::string end = description + " " + name + ";";
  if (currBlock)
    currBlock->addStmt(Stmt::comment(beginning + "warning: " + end));
  std::string hint = "";
  if (!unsetFlags.empty())
    hint = (" try adding " + ((rel == FlagRelation::And ? "all the " : "any ") +
                              ("flag(s) in: " + getFlagStr(unsetFlags))));
  errs() << beginning;
  (SmackOptions::ColoredWarnings ? errs().changeColor(raw_ostream::MAGENTA)
                                 : errs())
      << "warning: ";
  (SmackOptions::ColoredWarnings ? errs().resetColor() : errs())
      << end << hint << "\n";
}

void SmackWarnings::warnInfo(std::string info) {
  if (!isSufficientWarningLevel(WarningLevel::Info))
    return;
  errs() << "warning: " << info << "\n";
}
} // namespace smack
