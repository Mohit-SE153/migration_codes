CREATE TABLE [Purchasing].[PurchaseOrderDetail] (
    [PurchaseOrderID] int NOT NULL,
    [PurchaseOrderDetailID] int NOT NULL,
    [DueDate] datetime NOT NULL,
    [OrderQty] smallint NOT NULL,
    [ProductID] int NOT NULL,
    [UnitPrice] money NOT NULL,
    [LineTotal] money NOT NULL,
    [ReceivedQty] decimal(8,2) NOT NULL,
    [RejectedQty] decimal(8,2) NOT NULL,
    [StockedQty] decimal(9,2) NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
