CREATE TABLE [Production].[TransactionHistoryArchive] (
    [TransactionID] int NOT NULL,
    [ProductID] int NOT NULL,
    [ReferenceOrderID] int NOT NULL,
    [ReferenceOrderLineID] int NOT NULL,
    [TransactionDate] datetime NOT NULL,
    [TransactionType] nchar(1) NOT NULL,
    [Quantity] int NOT NULL,
    [ActualCost] money NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
